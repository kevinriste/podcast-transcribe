import os
from collections import defaultdict
from pydub import AudioSegment
import sys


def get_audio_length(audio_file_path):
    try:
        audio = AudioSegment.from_file(audio_file_path)
        return len(audio) or 0
    except Exception as e:
        print(f"Error reading audio file: {e}", file=sys.stderr)
        return 0


def find_duplicate_filenames_with_size(directory):
    seen = defaultdict(list)
    for filename in os.listdir(directory):
        full_path = os.path.join(directory, filename)
        if os.path.isfile(full_path):
            size = os.path.getsize(full_path)
            name = os.path.splitext(filename)[0][:-8]
            seen[name].append((filename, size))

    for name, file_list in seen.items():
        sizes = defaultdict(list)
        for file, size in file_list:
            sizes[size].append(file)
        for size, files in sizes.items():
            if len(files) > 1:
                for file in files:
                    print(f"{name}--{size}--{file}")


def find_duplicate_filenames_with_audio_length(directory):
    seen = defaultdict(list)
    for filename in os.listdir(directory):
        full_path = os.path.join(directory, filename)
        if os.path.isfile(full_path):
            name = os.path.splitext(filename)[0][:-8]
            seen[name].append((filename, full_path))

    for name, file_list in seen.items():
        audio_lengths = defaultdict(list)
        if len(file_list) > 1:
            for file, file_path in file_list:
                print(f"parsing audio length of {file_path}", file=sys.stderr)
                audio_length = format(
                    round(get_audio_length(file_path) / 1000.0 / 60.0, 2), ".2f"
                )
                print(f"audio_length: {audio_length}", file=sys.stderr)
                audio_lengths[audio_length].append(file)
            for audio_length, files in audio_lengths.items():
                # if len(files) > 1:
                for file in files:
                    print(f"{name}--{audio_length}--{file}")


if __name__ == "__main__":
    directory_path = "dropcaster-docker/audio"
    # find_duplicate_filenames_with_size(directory_path)
    find_duplicate_filenames_with_audio_length(directory_path)
