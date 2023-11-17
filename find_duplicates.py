import os


def find_duplicate_filenames_with_size(directory):
    seen = {}
    for filename in os.listdir(directory):
        full_path = os.path.join(directory, filename)
        if os.path.isfile(full_path):
            size = os.path.getsize(full_path)
            name = os.path.splitext(filename)[0][:-8]
            if name in seen:
                if seen[name] == size:
                    print(f"Duplicate: {filename} (Size: {size} bytes)")
            else:
                seen[name] = size


if __name__ == "__main__":
    directory_path = "dropcaster-docker/audio"
    find_duplicate_filenames_with_size(directory_path)
