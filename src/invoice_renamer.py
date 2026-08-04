import os
import re
import time
import shutil
from pypdf import PdfReader
from pdf2image import convert_from_path
import pytesseract
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import json

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

with open(CONFIG_PATH, "r") as f:
    CONFIG = json.load(f)

WATCH_DIR = os.path.expanduser(CONFIG["watch_dir"])
PROCESSED_DIR = os.path.expanduser(CONFIG["processed_dir"])

if CONFIG.get("tesseract_cmd"):
    pytesseract.pytesseract.tesseract_cmd = CONFIG["tesseract_cmd"]

POPPLER_PATH = CONFIG.get("poppler_path") or None
class InvoiceHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory or not event.src_path.lower().endswith(".pdf"):
            return

        print(f"New scan detected: {event.src_path}")
        self.process_pdf(event.src_path)

    def wait_for_stable_file(self, file_path, timeout=15, check_interval=0.5):
        """Wait until the file exists and its size stops changing, since
        scanners often write/rename files asynchronously."""
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
        """Return a list of text strings, one per page. Tries the embedded
        text layer first; OCRs any page that comes back blank."""
        page_texts = []

        try:
            reader = PdfReader(file_path)
            for page in reader.pages:
                page_texts.append(page.extract_text() or "")
        except Exception as e:
            print(f"pypdf text extraction failed: {e}")
            page_texts = []

        has_any_text = any(t.strip() for t in page_texts)
        needs_ocr = (not page_texts) or any(not t.strip() for t in page_texts)

        if not needs_ocr:
            return page_texts

        if not has_any_text:
            print("No text layer found, falling back to OCR for all pages...")
        else:
            print("Some pages missing text, falling back to OCR for those pages...")

        try:
            images = convert_from_path(file_path, dpi=300, poppler_path=POPPLER_PATH)
        except Exception as e:
            print(f"Failed to rasterize PDF for OCR: {e}")
            return page_texts  # return whatever partial text we had, if any

        if not page_texts:
            page_texts = ["" for _ in images]

        for i, image in enumerate(images):
            if i < len(page_texts) and not page_texts[i].strip():
                custom_config = r"--oem 3 --psm 6"
                page_texts[i] = pytesseract.image_to_string(image, config=custom_config)

        return page_texts

    def process_pdf(self, file_path):
        if not self.wait_for_stable_file(file_path):
            print(f"File never became available: {file_path}")
            return

        try:
            page_texts = self.extract_pages_text(file_path)

            if not page_texts or not any(t.strip() for t in page_texts):
                print("OCR produced no text. File left in incoming folder.")
                return

            invoice_numbers = []
            for i, text in enumerate(page_texts):
                matches = re.findall(r"\b\d{6}\b", text)
                if not matches:
                    matches = re.findall(r"Invoice\s*#?\s*:?\s*(\d{4,})", text, re.IGNORECASE)

                if matches:
                    invoice_numbers.append(matches[0])
                else:
                    print(f"Page {i + 1}: no invoice number found.")
                    print(f"--- OCR text preview (page {i + 1}) ---\n{text[:300]}\n------------------------")

            if not invoice_numbers:
                print("No invoice numbers extracted from any page. File left in incoming folder.")
                return

            invoices_str = " ".join(invoice_numbers)
            new_filename = f"{invoices_str}.pdf"

            os.makedirs(PROCESSED_DIR, exist_ok=True)
            destination = os.path.join(PROCESSED_DIR, new_filename)

            shutil.move(file_path, destination)
            print(f"Successfully processed -> {destination}")

        except Exception as e:
            print(f"Error reading file: {e}")


if __name__ == "__main__":
    os.makedirs(WATCH_DIR, exist_ok=True)
    event_handler = InvoiceHandler()
    observer = Observer()
    observer.schedule(event_handler, WATCH_DIR, recursive=False)

    print(f"Watching {WATCH_DIR}...")
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()