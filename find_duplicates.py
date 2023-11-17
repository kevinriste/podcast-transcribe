import os
from collections import defaultdict


def find_duplicate_filenames_with_size(directory):
    seen = defaultdict(list)
    for filename in os.listdir(directory):
        full_path = os.path.join(directory, filename)
        if os.path.isfile(full_path):
            size = os.path.getsize(full_path)
            name = os.path.splitext(filename)[0][:-8]
            seen[name].append((filename, size))

    for name, file_list in seen.items():
        if len(file_list) > 1 and len(set(size for _, size in file_list)) > 1:
            print(f"Matching pairs for '{name}':")
            for file, size in file_list:
                print(f"File: {file} (Size: {size} bytes)")


if __name__ == "__main__":
    directory_path = "dropcaster-docker/audio"
    find_duplicate_filenames_with_size(directory_path)
