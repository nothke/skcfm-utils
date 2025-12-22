import tkinter as tk
from tkinter import filedialog
from tkinter import ttk
import yt_dlp
import os
from mutagen.mp3 import MP3
from mutagen.easyid3 import EasyID3
import re
import threading
from pathlib import Path
from tkinterdnd2 import DND_FILES, TkinterDnD

video_url = "https://www.youtube.com/watch?v=7N8IDv8viZk"  # Replace with your video URL

def browse_folder():
    dir_selected = filedialog.askdirectory()
    if dir_selected:
        clear_and_set(dir_field, dir_selected)
        print(f"Selected folder: {dir_selected}")


def generate_meta():
    if file_field.get():
        filename = file_field.get()

        delimiters = [" - ", "(", "["]
        regex_pattern = "|".join(map(re.escape, delimiters))
        splits = re.split(regex_pattern, filename)

        print(splits)

        artist_field.delete(0, tk.END)
        artist_field.insert(0, splits[0].strip())
        if len(splits) > 1:
            title_field.delete(0, tk.END)
            title_field.insert(0, splits[1].strip())

def load_metadata_file(filepath):
    file_field.config(state="normal")
    clear_and_set(file_field, filepath)

    artist_field.config(state="normal")
    title_field.config(state="normal")

def my_hook(d):
    # print(f"\n\nSTATUS:{d['status']}\n\n")
    if d["status"] == "downloading":
        # Get progress information
        total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded_bytes = d.get("downloaded_bytes")
        speed = d.get("speed")
        eta = d.get("eta")

        root.update_idletasks()

        if total_bytes and downloaded_bytes:
            percent = (downloaded_bytes / total_bytes) * 100
            print(
                f"########## Downloading: {percent:.1f}% at {speed / 1024 / 1024:.2f} MiB/s, ETA: {eta} seconds"
            )
            progress_bar["value"] = percent
            progress_bar.update()

    elif d["status"] == "pre_process":
        print("Starting pre-processing...")

    elif d["status"] == "finished":
        print(f"\nDone downloading {d['filename']}")
        progress_bar["value"] = 100

        file_field.config(state="normal")

        filepath, _ = os.path.splitext(d["filename"])
        filepath += ".mp3"

        print(filepath)

        load_metadata_file(filepath)

        if should_set_meta.get():
            generate_meta()


def clear_and_set(field, str):
    field.delete(0, tk.END)
    field.insert(0, str)


def get_filepath():
    filename = file_field.get()
    return os.path.join(dir_field.get(), filename)


def read_meta():
    filename = get_filepath()
    _, extension = os.path.splitext(filename)

    if filename == "":
        print("filepath empty")
    elif not Path(filename).exists():
        print(f"file '{filename}' doesn't exist")
    elif extension != ".mp3":
        print(f"file not mp3 '{extension}'")
    else:
        print(f"Found '{filename}'")

        audio = MP3(filename, ID3=EasyID3)
        title = audio.get("title")
        artist = audio.get("artist")

        if title:
            print(f"Title: {title[0]}")
            clear_and_set(title_field, title[0])
        else:
            print("Title not found")

        if artist:
            print(f"Artist: {artist[0]}")
            clear_and_set(artist_field, artist[0])
        else:
            print("Artist not found")

        # print(f"Title: {}")
        # print(f"Artist: {audio['artist'][0]}")
        print(f"Length (seconds): {int(audio.info.length)}")


def write_meta():
    filename = get_filepath()
    _, extension = os.path.splitext(filename)

    if filename == "":
        print("filepath empty")
    elif not Path(filename).exists():
        print(f"file '{filename}' doesn't exist")
    elif extension != ".mp3":
        print(f"file not mp3 '{extension}'")
    else:
        audio = MP3(filename, ID3=EasyID3)
        audio["title"] = title_field.get()
        audio["artist"] = artist_field.get()
        audio.save()


# Define options (see yt-dlp documentation for all options)
ydl_opts = {
    "format": "bestaudio/best",
    "extract_audio": True,
    #'outtmpl': '%(title)s.%(ext)s',       # Output file name template
    "audio_format": "mp3",  # Set the final container format
    "audio_quality": 0,
    "progress_hooks": [my_hook],  # Optional: Add a progress hook
    "verbose": True,
    "postprocessors": [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",  # Specifies the target codec
            "preferredquality": "0",  # Specifies the quality (0 for best)
        }
    ],
}

# yt-dlp -x --audio-format best --audio-quality 0 URL


def show_entry_field_content():
    """Prints the current content of the entry field."""
    content = url_field.get()  # Get the content
    print("Input content:", content)


def download():
    video_url = url_field.get()

    if not should_be_playlist.get():
        video_url = video_url.split("&", 1)[0]

    print(f"Downloading from: '{video_url}'")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])


download_thread = threading.Thread(target=download)


def download_threaded():
    clear_and_set(file_field, "")
    progress_bar["value"] = 0
    progress_bar.update()

    download_thread = threading.Thread(target=download)
    download_thread.start()

def drop_in_file(event):
    load_metadata_file(root.tk.splitlist(event.data)[0])
    read_meta()

# Create the main window
root = TkinterDnD.Tk() #tk.Tk()
root.title("SKC.fm Downloader")
root.minsize(600, 100)

root.drop_target_register(DND_FILES)
root.dnd_bind('<<Drop>>', drop_in_file)

should_set_meta = tk.BooleanVar(value=True)
should_be_playlist = tk.BooleanVar(value=False)

### ------ Download ------

download_labelframe = ttk.Labelframe(root, text="Download", padding=(10, 5, 10, 10))
download_labelframe.pack(padx=10, pady=10, fill="both")
download_labelframe.columnconfigure(1, weight=1)

dir_frame = tk.Frame(download_labelframe)
dir_frame.pack(padx=10, pady=5, fill="both")
dir_frame.columnconfigure(0, weight=1)

dir_field = tk.Entry(dir_frame, width=30)
dir_field.insert(0, os.getcwd())
dir_field.grid(column=0, row=0, padx=0, pady=10, sticky="nsew")

browse_dir_button = tk.Button(dir_frame, text="Browse folder", command=browse_folder)
browse_dir_button.grid(column=1, row=0, padx=0, pady=10, sticky="nsew")

label = tk.Label(download_labelframe, text="COPY youtube, bandcamp, etc. URL HERE:")
label.pack(pady=3, padx=10)

url_field = tk.Entry(download_labelframe, width=30)
url_field.insert(0, video_url)
url_field.pack(pady=5, padx=10, fill="both")

checks_frame = tk.Frame(download_labelframe)
checks_frame.pack(padx=10, fill="both")
checks_frame.columnconfigure(0, weight=1)
checks_frame.columnconfigure(1, weight=1)

auto_meta_check = tk.Checkbutton(
    checks_frame, text="Auto meta", variable=should_set_meta
)
auto_meta_check.grid(row=0, column=0, sticky="w")

playlist_check = tk.Checkbutton(
    checks_frame, text="Playlist", variable=should_be_playlist
)
playlist_check.grid(row=1, column=0, sticky="w")

dl_button = tk.Button(checks_frame, text="Download", command=download_threaded)
dl_button.grid(row=0, column=1, rowspan=2, sticky="nsew")

progress_bar = ttk.Progressbar(
    download_labelframe,
    orient=tk.HORIZONTAL,
    length=200,
    mode="determinate",
    maximum=100,
)
progress_bar.pack(padx=10, pady=10, fill="both")

### ------ Metadata ------

meta_labelframe = ttk.Labelframe(root, text="Metadata", padding=(10, 5, 10, 10))
meta_labelframe.pack(padx=10, pady=10, fill="both")
# meta_labelframe.columnconfigure(1, weight=1)

fields_frame = tk.Frame(meta_labelframe)
fields_frame.pack(fill="both")
fields_frame.columnconfigure(1, weight=1)


def line_field(row, label):
    _label = tk.Label(fields_frame, text=label, width=10)
    _label.grid(row=row, column=0, padx=10, pady=10, sticky="nsew")
    _field = tk.Entry(fields_frame, width=30)
    _field.grid(row=row, column=1, padx=10, pady=10, sticky="nsew")
    _field.config(state="disabled")
    return _field


file_field = line_field(0, "File")
artist_field = line_field(1, "Artist")
title_field = line_field(2, "Title")

bottom_buttons_frame = tk.Frame(meta_labelframe)
bottom_buttons_frame.pack(fill="both")
bottom_buttons_frame.columnconfigure(1, weight=1)

def bottom_button(column, text, command):
    button = tk.Button(bottom_buttons_frame, text=text, command=command)
    button.grid(column=column, row=0, padx=5, pady=5, sticky="nsew")

bottom_button(0, "Load metadata", read_meta)
bottom_button(1, "Save metadata", write_meta)

root.mainloop()
