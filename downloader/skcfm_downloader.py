import tkinter as tk
from tkinter import filedialog
from tkinter import ttk
import yt_dlp
import os
import re
import threading
from pathlib import Path
from tkinterdnd2 import DND_FILES, TkinterDnD
import taglib
import requests
import time
import sys

if sys.getdefaultencoding().lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

print("Dobro veče")

video_url = "https://www.youtube.com/watch?v=7N8IDv8viZk"  # Replace with your video URL

pw = ("Sample", "sample")

try:
    with open("auth.txt", 'r') as file:
        spl = file.read().split(":")
        pw = (spl[0], spl[1])
except FileNotFoundError:
    print("Error: Auth file was not found.")
except Exception as e:
    print(f"An error occurred: {e}")

def post(name, command):
    cmd = "http://localhost:8081/commands"
    h = {"Content-Type": "text/plain; charset=utf-8"}

    request = requests.post(cmd, auth=pw, headers=h, data=command)

    print(f"POST {name}, command: '{command}', response: {request.text}")

    return request


def proppfrexx_ping():
    post("Ping", "PING")


def proppfrexx_queue_last():
    if path_is_valid():
        post("Append file", f"PLS_CURRENT_APPEND_FILE {get_filepath()}")
        time.sleep(0.5)

        print(f"Queued {get_filepath()} as last track in playlist")
        read_meta(set_fields=False)

def proppfrexx_queue_after_selected():
    if path_is_valid():
        get_selected = post("Index of next track", "PLS_CURRENT_GET_SELECTEDINDEX")
        num = int(get_selected.text) + 2

        print(f"Should be placed at {num}")

        post("Append file", f"PLS_CURRENT_APPEND_FILE {get_filepath()}")
        time.sleep(0.5)
        post("Select last", "PLS_CURRENT_SELECT_ENTRY LAST")
        time.sleep(0.5)
        post("Move after selected index", f"PLS_CURRENT_MOVE_TO {num}")

        print(f"Queued {get_filepath()} after selected")
        read_meta(set_fields=False)


def proppfrexx_set_next():
    if path_is_valid():
        get_next = post("Index of next track", "PLS_CURRENT_TRACKNEXT_GET")
        num = int(get_next.text) - 1

        print(f"Should be placed at {num}")

        post("Append file", f"PLS_CURRENT_APPEND_FILE {get_filepath()}")
        time.sleep(0.5)
        post("Select last", "PLS_CURRENT_SELECT_ENTRY LAST")
        time.sleep(0.5)
        post("Move to next index", f"PLS_CURRENT_MOVE_TO {num}")
        time.sleep(0.5)
        post("Load track", "PLS_CURRENT_LOAD_SELECTED")

        print(f"Queued {get_filepath()} as next track")
        read_meta(set_fields=False)


def proppfrexx_play_now():
    if path_is_valid():
        proppfrexx_set_next()

        time.sleep(0.5)
        post("Load track", "PLS_CURRENT_PLAY_NEXT")

        print(f"Playing {get_filepath()} now! (..with crossfade)")
        read_meta(set_fields=False)


def browse_folder():
    dir_selected = filedialog.askdirectory(initialdir=dir_field.get())
    if dir_selected:
        clear_and_set(dir_field, dir_selected)
        print(f"Selected folder: {dir_selected}")


def load_file(filepath):
    file_field.config(state="normal")
    clear_and_set(file_field, filepath)

    artist_field.config(state="normal")
    title_field.config(state="normal")


def ytdlp_hook(d):
    # print(f"\n\nSTATUS:{d['status']}\n\n")
    if d["status"] == "downloading":
        # Get progress information
        total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded_bytes = d.get("downloaded_bytes")
        speed = d.get("speed")
        eta = d.get("eta")

        root.update_idletasks()

        if total_bytes and downloaded_bytes and speed and eta:
            percent = (downloaded_bytes / total_bytes) * 100
            print(
                f"########## Downloading: {percent:.1f}% at {speed / 1024 / 1024:.2f} MiB/s, ETA: {eta} seconds"
            )
            progress_bar["value"] = percent
            progress_bar.update()

    elif d["status"] == "pre_process":
        print("Starting pre-processing...")

    elif d["status"] == "finished":
        print(f"\nSKCSKCSKC: Done downloading {d['filename']}")


def ytdlp_postprocessor_hook(d):
    if d["status"] == "started":
        print(f"SKCSKCSKC: Post-processor started: {d.get('pp', {}).get('__name__')}")
        progress_bar["value"] = 50
        progress_bar.update()
    elif d["status"] == "finished":
        print(f"Final file location: {d['info_dict']['filepath']}")

        # print("\n\n================================\n\n")
        # safe_string = str(d).encode(sys.stdout.encoding, errors='replace').decode(sys.stdout.encoding)
        # print(safe_string)
        # print("\n\n================================\n\n")

        progress_bar["value"] = 100
        progress_bar.update()

        filepath = d["info_dict"]["filepath"]  # this is ffmpeg specific

        filename, ext = os.path.splitext(filepath)
        if ext != ".mp3":
            print(f"Error: Expected .mp3 found {ext}")
            filepath = filename + ".mp3"

        print(f"\nSKCSKCSKC: Done post processing {filepath}")

        load_file(filepath)

        if should_set_meta.get():
            auto_meta_from_filename()
            write_meta()

    elif d["status"] == "processing":
        print(
            f"SKCSKCSKC: Processing progress: {d.get('progress_stats', {}).get('percentage')}%"
        )
        percent = d.get("progress_stats", {}).get("percentage")
        progress_bar["value"] = int(percent)
        progress_bar.update()


def clear_and_set(field, str):
    field.delete(0, tk.END)
    field.insert(0, str)


def path_is_valid():
    filename = get_filepath()
    if filename == "":
        print("filepath empty")
        return False
    elif not Path(filename).exists():
        print(f"file '{filename}' doesn't exist")
        return False
    else:
        return True


def get_filepath():
    filename = file_field.get()
    return os.path.join(dir_field.get(), filename)


def get_filename_only():
    p = Path(file_field.get())
    return p.stem

def read_meta(set_fields=True):
    filename = get_filepath()

    if filename == "":
        print("filepath empty")
    elif not Path(filename).exists():
        print(f"file '{filename}' doesn't exist")
    else:
        with taglib.File(filename, save_on_exit=False) as song:
            artist = song.tags["ARTIST"]
            title = song.tags["TITLE"]

            if title:
                print(f"Title: {title[0]}")
                if set_fields:
                    clear_and_set(title_field, title[0])
            else:
                print("Title not found")

            if artist:
                print(f"Artist: {artist[0]}")
                if set_fields:
                    clear_and_set(artist_field, artist[0])
            else:
                print("Artist not found")


def write_meta():
    filename = get_filepath()

    if filename == "":
        print("filepath empty")
    elif not Path(filename).exists():
        print(f"file '{filename}' doesn't exist")
    else:
        with taglib.File(filename, save_on_exit=True) as song:
            song.tags["ARTIST"] = [artist_field.get()]
            song.tags["TITLE"] = [title_field.get()]

        print(f"Metadata set to file '{filename}':\nArtist: '{artist_field.get()}'\nTitle: '{title_field.get()}'")


def auto_meta_from_filename():
    filename = get_filename_only()

    if filename:
        regex_hyphen = "|".join(map(re.escape, [" - "]))
        regex_parenth = "|".join(map(re.escape, ["(", "["]))
        splits = re.split(regex_hyphen, filename)

        if len(splits) > 1:
            title_splits = re.split(regex_parenth, splits[1])
            clear_and_set(artist_field, splits[0].strip())
            clear_and_set(title_field, title_splits[0].strip())
        else:
            title_splits = re.split(regex_parenth, splits[0])
            clear_and_set(title_field, title_splits[0].strip())


# Define options (see yt-dlp documentation for all options)
ydl_opts = {
    "format": "bestaudio/best",
    "extract_audio": True,
    #'outtmpl': '%(title)s.%(ext)s',       # Output file name template
    "audio_format": "mp3",  # Set the final container format
    "audio_quality": 0,
    "progress_hooks": [ytdlp_hook],  # Optional: Add a progress hook
    "verbose": True,
    "paths": {
        "home": os.getcwd(),
    },
    "noplaylist": True,          # download single video if URL is a playlist item
    "postprocessors": [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",  # Specifies the target codec
            "preferredquality": "0",  # Specifies the quality (0 for best)
        }
    ],
    "postprocessor_hooks": [ytdlp_postprocessor_hook],
}

# yt-dlp -x --audio-format best --audio-quality 0 URL


def show_entry_field_content():
    """Prints the current content of the entry field."""
    content = url_field.get()  # Get the content
    print("Input content:", content)


def download():
    video_url = url_field.get()\

    ydl_opts["paths"]["home"] = dir_field.get()

    progress_bar["value"] = 5
    progress_bar.update()

    playlist = should_be_playlist.get()

    ydl_opts["noplaylist"] = not playlist
    # video_url = video_url.split("&", 1)[0]

    if playlist:
        print(f"Downloading playlist from: '{video_url}'")
    else:
        print(f"Downloading single-track from: '{video_url}'")

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
    load_file(root.tk.splitlist(event.data)[0])
    read_meta()


# Create the main window
root = TkinterDnD.Tk()  # tk.Tk()
root.title("SKC.fm Downloader 5000 - Mišatel TRIAL LICENSE - Expires in NaN")
root.minsize(600, 100)

# style = ttk.Style()
# style.theme_use('clam')

root.drop_target_register(DND_FILES)
root.dnd_bind("<<Drop>>", drop_in_file)

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
    checks_frame, text="Auto meta from filename", variable=should_set_meta
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


def line_field(row, label, enable=False):
    _label = tk.Label(fields_frame, text=label, width=10)
    _label.grid(row=row, column=0, padx=10, pady=10, sticky="nsew")
    _field = tk.Entry(fields_frame, width=30)
    _field.grid(row=row, column=1, padx=10, pady=10, sticky="nsew")
    if not enable:
        _field.config(state="disabled")
    return _field


file_field = line_field(0, "File")
artist_field = line_field(1, "Artist", enable=True)
title_field = line_field(2, "Title")

bottom_buttons_frame = tk.Frame(meta_labelframe)
bottom_buttons_frame.pack(fill="both")
bottom_buttons_frame.columnconfigure(1, weight=1)


def bottom_button(column, text, command):
    button = tk.Button(bottom_buttons_frame, text=text, command=command)
    button.grid(column=column, row=0, padx=5, pady=5, sticky="nsew")


bottom_button(0, "Load metadata", read_meta)
bottom_button(1, "Save metadata", write_meta)

### ------ ProppFrexx control ------

proppfrexx_labelframe = ttk.Labelframe(
    root, text="ProppFrexx control", padding=(10, 5, 10, 10)
)
proppfrexx_labelframe.pack(padx=10, pady=10, fill="both")
proppfrexx_labelframe.columnconfigure(3, weight=1)


def pf_button(column, text, command):
    button = tk.Button(proppfrexx_labelframe, text=text, command=command)
    button.grid(column=column, row=0, padx=5, pady=5, sticky="nsew")


pf_button(0, "Ping", proppfrexx_ping)
pf_button(1, "Queue last", proppfrexx_queue_last)
pf_button(2, "Queue after selected", proppfrexx_queue_after_selected)
pf_button(3, "Queue next", proppfrexx_set_next)
pf_button(4, "Play NOW", proppfrexx_play_now)

footer = tk.Label(root, text="Powered by Mišatel")
footer.pack(padx=10, pady=10)

root.mainloop()