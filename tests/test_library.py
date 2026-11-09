import os
import tempfile
import unittest

from photo_wall.library import collect_images, natural_key


class LibraryTests(unittest.TestCase):
    def test_natural_sort_orders_numbers_by_value(self):
        names = ["img10.jpg", "img2.jpg", "img1.jpg"]
        self.assertEqual(sorted(names, key=natural_key), ["img1.jpg", "img2.jpg", "img10.jpg"])

    def test_folder_loads_images_but_not_subfolders_or_hidden_files(self):
        with tempfile.TemporaryDirectory() as folder:
            self._touch(folder, "b.jpg")
            self._touch(folder, "a10.PNG")
            self._touch(folder, "a2.png")
            self._touch(folder, "notes.txt")
            self._touch(folder, ".secret.jpg")
            self._touch(folder, "._ignored.jpg")
            nested = os.path.join(folder, "album")
            os.makedirs(nested)
            self._touch(nested, "inside.jpg")

            found = collect_images([folder])
            names = [os.path.basename(path) for path in found]
            self.assertEqual(names, ["a2.png", "a10.PNG", "b.jpg"])

    def test_recursive_mode_loads_nested_images_and_skips_hidden_dirs(self):
        with tempfile.TemporaryDirectory() as folder:
            self._touch(folder, "root.jpg")
            album = os.path.join(folder, "album")
            nested = os.path.join(album, "roll2")
            os.makedirs(nested)
            self._touch(album, "img2.jpg")
            self._touch(album, "img10.jpg")
            self._touch(nested, "frame.png")
            hidden = os.path.join(folder, ".hidden")
            os.makedirs(hidden)
            self._touch(hidden, "secret.jpg")

            shallow = [os.path.basename(path) for path in collect_images([folder], recursive=False)]
            self.assertEqual(shallow, ["root.jpg"])

            deep = [os.path.relpath(path, folder) for path in collect_images([folder], recursive=True)]
            self.assertEqual(
                deep,
                [
                    os.path.join("album", "img2.jpg"),
                    os.path.join("album", "img10.jpg"),
                    os.path.join("album", "roll2", "frame.png"),
                    "root.jpg",
                ],
            )

    def test_explicit_files_can_keep_selection_order(self):
        with tempfile.TemporaryDirectory() as folder:
            first = self._touch(folder, "z.jpg")
            second = self._touch(folder, "a.jpg")
            found = collect_images([second, first, second], sort=False)
            self.assertEqual(found, [second, first])

    def _touch(self, folder, name):
        path = os.path.join(folder, name)
        with open(path, "wb") as handle:
            handle.write(b"")
        return path


if __name__ == "__main__":
    unittest.main()
