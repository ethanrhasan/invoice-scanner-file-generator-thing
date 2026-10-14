import os
import re
import time
import shutil
from pypdf import PdfReader, PdfWriter
from pdf2image import convert_from_path
from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPageCountError, PDFSyntaxError
import numpy as np
from PIL import Image, ImageOps
import pytesseract
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import json

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

VALID_JSON_ESCAPES = set('"\\/bfnrtu')


def _fix_stray_backslashes(text):
    """Double up backslashes that aren't part of a valid JSON escape sequence.

    Lets people hand-edit Windows paths like "C:\\Users\\me" into config.json
    without needing to know JSON escaping rules.
    """
    return re.sub(
        r"\\(.)",
        lambda m: m.group(0) if m.group(1) in VALID_JSON_ESCAPES else "\\\\" + m.group(1),
        text,
    )


def load_config():
    with open(CONFIG_PATH, "r") as f:
        raw = f.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        if e.msg.startswith("Invalid \\escape"):
            fixed = _fix_stray_backslashes(raw)
            try:
                config = json.loads(fixed)
            except json.JSONDecodeError:
                raise e
            with open(CONFIG_PATH, "w") as f:
                f.write(fixed)
            return config
        raise


def _has_pdfinfo(directory):
    return any(
        os.path.isfile(os.path.join(directory, name)) for name in ("pdfinfo.exe", "pdfinfo")
    )


def resolve_poppler_path(poppler_path, log=print):
    """Find the folder that actually contains pdfinfo.

    People often point poppler_path at the extracted poppler folder instead of
    its Library\\bin, so look inside it before giving up. Returns None to fall
    back to PATH.
    """
    if not poppler_path:
        if not shutil.which("pdfinfo"):
            log("Warning: poppler_path is not set and poppler is not on PATH, OCR will fail.")
        return None

    poppler_path = os.path.expanduser(poppler_path.strip().strip('"'))
    if not os.path.isdir(poppler_path):
        log(f"Warning: poppler_path does not exist: {poppler_path}")
        return poppler_path

    if _has_pdfinfo(poppler_path):
        return poppler_path

    for root, _dirs, _files in os.walk(poppler_path):
        if _has_pdfinfo(root):
            log(f"Using poppler binaries found at: {root}")
            return root

    log(f"Warning: no pdfinfo.exe found in poppler_path ({poppler_path}). "
        "It should point to poppler's Library\\bin folder.")
    return poppler_path


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


def _line_mask(dark, length, axis):
    """Mark pixels that belong to a run of at least `length` dark pixels
    along `axis` (1 = horizontal lines, 0 = vertical lines)."""
    n = dark.shape[axis]
    if n < length:
        return np.zeros_like(dark)
    pad = [(0, 0), (0, 0)]
    pad[axis] = (1, 0)

    counts = np.pad(np.cumsum(dark, axis=axis, dtype=np.int32), pad)
    window = (np.take(counts, range(length, n + 1), axis=axis)
              - np.take(counts, range(0, n - length + 1), axis=axis))
    starts = np.pad(np.cumsum(window >= length * 0.9, axis=axis, dtype=np.int32), pad)

    # a pixel is on a line if any qualifying window starts within `length` before it
    idx = np.arange(n)
    hi = np.clip(idx + 1, 0, n - length + 1)
    lo = np.clip(idx - length + 1, 0, n - length + 1)
    return (np.take(starts, hi, axis=axis) - np.take(starts, lo, axis=axis)) > 0


def preprocess_for_ocr(image, dpi, threshold):
    """Black and white the page and erase table borders.

    Tesseract tends to drop text sitting inside boxes (like the Invoice # cell),
    so removing long straight lines helps a lot.
    """
    dark = np.asarray(ImageOps.grayscale(image)) < threshold
    min_line = max(dpi // 3, 20)  # anything straighter than 1/3 inch is a border, not text
    lines = _line_mask(dark, min_line, 1) | _line_mask(dark, min_line, 0)
    lines = lines | np.roll(lines, 1, 0) | np.roll(lines, -1, 0) | np.roll(lines, 1, 1) | np.roll(lines, -1, 1)
    return Image.fromarray(np.where(dark & ~lines, 0, 255).astype(np.uint8))


# (threshold, psm) passes tried in order until one finds an invoice number
OCR_PASSES = [(160, 11), (120, 11), (200, 11), (160, 6), (200, 6)]


def find_invoice_number(text, length=6):
    # ignore references to other invoices like "back order to inv#140897"
    text = re.sub(r"\binv\s*#\s*\d+", "", text, flags=re.IGNORECASE)

    labeled = re.findall(rf"Invoice\s*(?:#|No\.?)?\s*:?\s*(\d{{{length}}})\b", text, re.IGNORECASE)
    if labeled:
        return labeled[0]

    matches = re.findall(rf"\b\d{{{length}}}\b", text)
    return matches[0] if matches else None


class InvoiceHandler(FileSystemEventHandler):
    def __init__(self, config, log=print, confirm_delete=None):
        super().__init__()
        self.log = log
        self.confirm_delete = confirm_delete or default_confirm_delete
        self.watch_dir = os.path.expanduser(config["watch_dir"])
        self.processed_dir = os.path.expanduser(config["processed_dir"])
        self.poppler_path = resolve_poppler_path(config.get("poppler_path"), log=log)
        self.ocr_dpi = int(config.get("ocr_dpi", 300))
        self.number_length = int(config.get("invoice_number_length", 6))

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
        # a text layer without an invoice number is usually a bad scanner OCR, so redo those too
        needs_ocr = (not page_texts) or any(
            not find_invoice_number(t, self.number_length) for t in page_texts
        )

        if not needs_ocr:
            return page_texts

        if not has_any_text:
            self.log("No text layer found, falling back to OCR for all pages...")
        else:
            self.log("Some pages have no invoice number in their text, falling back to OCR for those pages...")

        try:
            images = convert_from_path(file_path, dpi=self.ocr_dpi, poppler_path=self.poppler_path)
        except PDFInfoNotInstalledError:
            self.log("Failed to rasterize PDF for OCR: poppler could not be found. "
                     f"Check poppler_path in config.json (currently: {self.poppler_path}), "
                     "it should be the folder containing pdfinfo.exe (poppler's Library\\bin).")
            return page_texts
        except (PDFPageCountError, PDFSyntaxError) as e:
            self.log(f"Failed to rasterize PDF for OCR, the PDF looks damaged, "
                     f"password protected, or still being written by the scanner: {e}")
            return page_texts
        except Exception as e:
            self.log(f"Failed to rasterize PDF for OCR ({type(e).__name__}): {e}")
            return page_texts  # return whatever partial text we had, if any

        if not page_texts:
            page_texts = ["" for _ in images]

        for i, image in enumerate(images):
            if i < len(page_texts) and not find_invoice_number(page_texts[i], self.number_length):
                page_texts[i] = self.ocr_page(image) or page_texts[i]

        return page_texts

    def ocr_page(self, image):
        """Run OCR passes until one finds an invoice number, most pages only need the first."""
        text = ""
        for threshold, psm in OCR_PASSES:
            cleaned = preprocess_for_ocr(image, self.ocr_dpi, threshold)
            text = pytesseract.image_to_string(cleaned, config=f"--oem 3 --psm {psm}")
            if find_invoice_number(text, self.number_length):
                return text
        return text

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
                number = find_invoice_number(text, self.number_length)
                if number:
                    page_numbers.append(number)
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
