import os
import re
import time
import shutil
from pypdf import PdfReader, PdfWriter
from pdf2image import convert_from_path
import pytesseract
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import json

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=4)


def default_confirm_delete(original_path, destination_paths):
    outputs = "\n".join(destination_paths)
    try:
        answer = input(
            f"Output saved to:\n{outputs}\nDelete the original scan ({original_path})? [y/N]: "
        ).strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


class InvoiceHandler(FileSystemEventHandler):
    def __init__(self, config, log=print, confirm_delete=None):
        super().__init__()
        self.log = log
        self.confirm_delete = confirm_delete or default_confirm_delete
        self.watch_dir = os.path.expanduser(config["watch_dir"])
        self.processed_dir = os.path.expanduser(config["processed_dir"])
        poppler_path = config.get("poppler_path")
        self.poppler_path = os.path.expanduser(poppler_path) if poppler_path else None

        if config.get("tesseract_cmd"):
            pytesseract.pytesseract.tesseract_cmd = os.path.expanduser(config["tesseract_cmd"])

    def on_created(self, event):
        if event.is_directory or not event.src_path.lower().endswith(".pdf"):
            return

        self.log(f"New scan detected: {event.src_path}")
        self.process_pdf(event.src_path)

    def wait_for_stable_file(self, file_path, timeout=15, check_interval=0.5):
        start = time.time()
        last_size = -1

        while time.time() - start < timeout:
            if os.path.exists(file_path):
                current_size = os.path.getsize(file_path)
                if current_size == last_size and current_size > 0:
                    return True
                last_size = current_size
            time.sleep(check_interval)

        return os.path.exists(file_path)

    def extract_pages_text(self, file_path):
        page_texts = []

        try:
            reader = PdfReader(file_path)
            for page in reader.pages:
                page_texts.append(page.extract_text() or "")
        except Exception as e:
            self.log(f"pypdf text extraction failed: {e}")
            page_texts = []

        has_any_text = any(t.strip() for t in page_texts)
        needs_ocr = (not page_texts) or any(not t.strip() for t in page_texts)

        if not needs_ocr:
            return page_texts

        if not has_any_text:
            self.log("No text layer found, falling back to OCR for all pages...")
        else:
            self.log("Some pages missing text, falling back to OCR for those pages...")

        try:
            images = convert_from_path(file_path, dpi=300, poppler_path=self.poppler_path)
        except Exception as e:
            self.log(f"Failed to rasterize PDF for OCR: {e}")
            return page_texts  # return whatever partial text we had, if any

        if not page_texts:
            page_texts = ["" for _ in images]

        for i, image in enumerate(images):
            if i < len(page_texts) and not page_texts[i].strip():
                custom_config = r"--oem 3 --psm 6"
                page_texts[i] = pytesseract.image_to_string(image, config=custom_config)

        return page_texts

    def _group_consecutive_pages(self, page_numbers):
        """Group page indexes into runs where consecutive pages share the
        same detected number, e.g. a 2-page invoice repeating its number on
        both pages becomes a single group instead of two."""
        groups = []
        start = 0
        current = page_numbers[0]

        for i in range(1, len(page_numbers)):
            if page_numbers[i] != current:
                groups.append((current, start, i - 1))
                start = i
                current = page_numbers[i]

        groups.append((current, start, len(page_numbers) - 1))
        return groups

    def _unique_destination(self, number):
        destination = os.path.join(self.processed_dir, f"{number}.pdf")
        counter = 2
        while os.path.exists(destination):
            destination = os.path.join(self.processed_dir, f"{number} ({counter}).pdf")
            counter += 1
        return destination

    def process_pdf(self, file_path):
        if not self.wait_for_stable_file(file_path):
            self.log(f"File never became available: {file_path}")
            return

        try:
            page_texts = self.extract_pages_text(file_path)

            if not page_texts or not any(t.strip() for t in page_texts):
                self.log("OCR produced no text. File left in incoming folder.")
                return

            page_numbers = []
            for i, text in enumerate(page_texts):
                matches = re.findall(r"\b\d{6}\b", text)
                if not matches:
                    matches = re.findall(r"Invoice\s*#?\s*:?\s*(\d{4,})", text, re.IGNORECASE)

                if matches:
                    page_numbers.append(matches[0])
                else:
                    page_numbers.append(None)
                    self.log(f"Page {i + 1}: no invoice number found.")
                    self.log(f"--- OCR text preview (page {i + 1}) ---\n{text[:300]}\n------------------------")

            if not any(page_numbers):
                self.log("No invoice numbers extracted from any page. File left in incoming folder.")
                return

            os.makedirs(self.processed_dir, exist_ok=True)
            reader = PdfReader(file_path)

            written_files = []
            for number, start, end in self._group_consecutive_pages(page_numbers):
                if number is None:
                    page_range = f"page {start + 1}" if start == end else f"pages {start + 1}-{end + 1}"
                    self.log(f"Skipped {page_range}: no invoice number found, not included in any output file.")
                    continue

                destination = self._unique_destination(number)
                writer = PdfWriter()
                for page_index in range(start, end + 1):
                    writer.add_page(reader.pages[page_index])
                with open(destination, "wb") as f:
                    writer.write(f)

                written_files.append(destination)
                page_range = f"page {start + 1}" if start == end else f"pages {start + 1}-{end + 1}"
                self.log(f"Successfully processed ({page_range}) -> {destination}")

            if not written_files:
                self.log("No invoice numbers extracted from any page. File left in incoming folder.")
                return

            if self.confirm_delete(file_path, written_files):
                try:
                    os.remove(file_path)
                    self.log(f"Deleted original scan: {file_path}")
                except Exception as e:
                    self.log(f"Could not delete original scan: {e}")
            else:
                self.log(f"Kept original scan in input folder: {file_path}")

        except Exception as e:
            self.log(f"Error reading file: {e}")


def run_watcher(config, log=print, stop_event=None, confirm_delete=None):
    watch_dir = os.path.expanduser(config["watch_dir"])
    os.makedirs(watch_dir, exist_ok=True)

    event_handler = InvoiceHandler(config, log=log, confirm_delete=confirm_delete)
    observer = Observer()
    observer.schedule(event_handler, watch_dir, recursive=False)

    log(f"Watching {watch_dir}...")
    observer.start()
    try:
        while not (stop_event and stop_event.is_set()):
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        log("Stopped watching.")


if __name__ == "__main__":
    run_watcher(load_config())
