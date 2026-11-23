import io
import tarfile

import pytest

from covenant.git_archive import extract_git_archive


@pytest.mark.parametrize("include_link", [False, True])
def test_git_archive_on_early_python(tmp_path, monkeypatch, include_link):
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        contents = b"example\n"
        item = tarfile.TarInfo("src/example.txt")
        item.size = len(contents)
        archive.addfile(item, io.BytesIO(contents))
        if include_link:
            link = tarfile.TarInfo("src/alias.txt")
            link.type = tarfile.SYMTYPE
            link.linkname = "example.txt"
            archive.addfile(link)

    if include_link:
        with pytest.raises(ValueError, match="unsupported entry"):
            extract_git_archive(data.getvalue(), tmp_path)
        assert not (tmp_path / "src" / "alias.txt").exists()
    else:
        extract_git_archive(data.getvalue(), tmp_path)
    assert (tmp_path / "src" / "example.txt").read_bytes() == contents
