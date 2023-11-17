import os


def find_duplicate_filenames(directory):
    seen = {}
    for filename in os.listdir(directory):
        name = os.path.splitext(filename)[0][:-8]
        if name in seen:
            print(filename)
        else:
            seen[name] = None


if __name__ == "__main__":
    directory_path = "dropcaster-docker/audio"  # Replace with your directory path
    find_duplicate_filenames(directory_path)
