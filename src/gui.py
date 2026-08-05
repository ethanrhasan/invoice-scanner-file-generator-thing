import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import invoice_renamer as core


class InvoiceRenamerGUI:
    def __init__(self, root):
        self.root = root
        root.title("Invoice Renamer")
        root.geometry("640x480")
        root.minsize(560, 400)

        self.config = core.load_config()
        self.log_queue = queue.Queue()
        self.confirm_queue = queue.Queue()
        self.stop_event = None
        self.worker_thread = None

        self.watch_dir_var = tk.StringVar(value=self.config.get("watch_dir", ""))
        self.processed_dir_var = tk.StringVar(value=self.config.get("processed_dir", ""))
        self.status_var = tk.StringVar(value="Stopped")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_log_queue()
        self._poll_confirm_queue()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        paths_frame = ttk.LabelFrame(self.root, text="Folders")
        paths_frame.pack(fill="x", **pad)
        paths_frame.columnconfigure(1, weight=1)

        ttk.Label(paths_frame, text="Watch (scan input) folder:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(paths_frame, textvariable=self.watch_dir_var).grid(row=0, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(paths_frame, text="Browse...", command=self._browse_watch_dir).grid(row=0, column=2, padx=6, pady=4)

        ttk.Label(paths_frame, text="Output (renamed files) folder:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(paths_frame, textvariable=self.processed_dir_var).grid(row=1, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(paths_frame, text="Browse...", command=self._browse_processed_dir).grid(row=1, column=2, padx=6, pady=4)

        controls_frame = ttk.Frame(self.root)
        controls_frame.pack(fill="x", **pad)

        self.start_button = ttk.Button(controls_frame, text="Start Watching", command=self._start)
        self.start_button.pack(side="left")

        self.stop_button = ttk.Button(controls_frame, text="Stop", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)

        ttk.Label(controls_frame, text="Status:").pack(side="left", padx=(20, 4))
        ttk.Label(controls_frame, textvariable=self.status_var).pack(side="left")

        log_frame = ttk.LabelFrame(self.root, text="Activity Log")
        log_frame.pack(fill="both", expand=True, **pad)

        self.log_text = tk.Text(log_frame, state="disabled", wrap="word")
        self.log_text.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _browse_watch_dir(self):
        chosen = filedialog.askdirectory(title="Select watch (input) folder")
        if chosen:
            self.watch_dir_var.set(chosen)
            self._save_paths()

    def _browse_processed_dir(self):
        chosen = filedialog.askdirectory(title="Select output folder")
        if chosen:
            self.processed_dir_var.set(chosen)
            self._save_paths()

    def _save_paths(self):
        self.config["watch_dir"] = self.watch_dir_var.get()
        self.config["processed_dir"] = self.processed_dir_var.get()
        core.save_config(self.config)
        self._log(f"Saved settings. Output folder set to: {self.config['processed_dir']}")

    def _log(self, message):
        self.log_queue.put(message)

    def _poll_log_queue(self):
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(150, self._poll_log_queue)

    def _poll_confirm_queue(self):
        try:
            request = self.confirm_queue.get_nowait()
        except queue.Empty:
            request = None

        if request is not None:
            original_path, destination_paths, result_holder, done_event = request
            outputs = "\n".join(destination_paths)
            answer = messagebox.askyesno(
                "Delete original scan?",
                f"Output saved to:\n{outputs}\n\n"
                f"Delete the original scan from the input folder?\n{original_path}",
            )
            result_holder.append(answer)
            done_event.set()

        self.root.after(150, self._poll_confirm_queue)

    def _confirm_delete(self, original_path, destination_paths):
        """Called from the watcher thread; blocks it until the user answers
        the popup shown on the main thread."""
        done_event = threading.Event()
        result_holder = []
        self.confirm_queue.put((original_path, destination_paths, result_holder, done_event))
        done_event.wait()
        return result_holder[0] if result_holder else False

    def _start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return

        self._save_paths()

        watch_dir = self.watch_dir_var.get().strip()
        processed_dir = self.processed_dir_var.get().strip()
        if not watch_dir or not processed_dir:
            messagebox.showerror("Missing folder", "Please set both the watch and output folders.")
            return

        self.stop_event = threading.Event()
        run_config = dict(self.config)
        self.worker_thread = threading.Thread(
            target=self._run_watcher_thread,
            args=(run_config, self.stop_event),
            daemon=True,
        )
        self.worker_thread.start()

        self.status_var.set("Watching")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

    def _run_watcher_thread(self, run_config, stop_event):
        try:
            core.run_watcher(
                run_config,
                log=self._log,
                stop_event=stop_event,
                confirm_delete=self._confirm_delete,
            )
        except Exception as e:
            self._log(f"Watcher stopped with an error: {e}")
        finally:
            self.root.after(0, self._on_watcher_stopped)

    def _on_watcher_stopped(self):
        self.status_var.set("Stopped")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

    def _stop(self):
        if self.stop_event:
            self.stop_event.set()
        self.status_var.set("Stopping...")
        self.stop_button.configure(state="disabled")

    def _on_close(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self._stop()
            self._decline_pending_confirms()
            self.worker_thread.join(timeout=5)
        self.root.destroy()

    def _decline_pending_confirms(self):
        """Auto-answer 'No' to any delete prompts still queued so the
        watcher thread doesn't block forever while the window is closing."""
        while True:
            try:
                _, _, result_holder, done_event = self.confirm_queue.get_nowait()
            except queue.Empty:
                break
            result_holder.append(False)
            done_event.set()


def main():
    root = tk.Tk()
    InvoiceRenamerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
