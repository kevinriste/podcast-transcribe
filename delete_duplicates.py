import os


def delete_files_from_list(file_list):
    with open(file_list, "r") as f:
        for line in f:
            filename = line.strip()
            if os.path.exists(filename):
                os.remove(filename)
                print(f"Deleted: {filename}")
            else:
                print(f"File not found: {filename}")


file_list = "../../duplicates10-final-edited.txt"  # Replace with your text file name
delete_files_from_list(file_list)
