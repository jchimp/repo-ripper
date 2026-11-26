"""Secret storage and the folder browser's security rule."""
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import fsbrowse, secrets_store
from app.config import get_settings

KEY_A = "a" * 40
KEY_B = "b" * 40


# --- secrets ----------------------------------------------------------------

def test_secret_round_trip() -> None:
    stored = secrets_store.encrypt("ghp_example", KEY_A)
    assert "ghp_example" not in stored
    assert secrets_store.decrypt(stored, KEY_A) == "ghp_example"


def test_secret_with_wrong_key_is_unreadable() -> None:
    stored = secrets_store.encrypt("ghp_example", KEY_A)
    with pytest.raises(secrets_store.SecretUnreadable):
        secrets_store.decrypt(stored, KEY_B)


def test_damaged_secret_is_unreadable() -> None:
    with pytest.raises(secrets_store.SecretUnreadable):
        secrets_store.decrypt("not a fernet value", KEY_A)


# --- folder browser ---------------------------------------------------------

@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """One allowed root with a sub-folder and a file; `outside` is next to it."""
    root = tmp_path / "root"
    (root / "nas" / "git").mkdir(parents=True)
    (root / "nas" / "notes.txt").write_text("a file")
    (tmp_path / "outside" / "private").mkdir(parents=True)
    monkeypatch.setenv("BROWSE_ROOTS", str(root))
    get_settings.cache_clear()
    yield root.resolve()
    get_settings.cache_clear()


def test_listing_shows_folders_only(root: Path) -> None:
    listing = fsbrowse.list_dir(str(root / "nas"))
    assert [d.label for d in listing.dirs] == ["git"]
    assert [c.label for c in listing.crumbs] == [str(root), "nas"]
    assert listing.parent == str(root)
    # The root itself has no parent inside the allowed area.
    assert fsbrowse.list_dir(str(root)).parent is None


def test_roots_listing_reports_missing_roots(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSE_ROOTS", f"{root}, {root.parent / 'not-mounted'}")
    get_settings.cache_clear()
    listing = fsbrowse.roots_listing()
    assert [d.path for d in listing.dirs] == [str(root)]
    assert listing.missing_roots == [str(root.parent / "not-mounted")]


@pytest.mark.parametrize("suffix", [
    "..", "../outside", "nas/../../outside/private", "nas/git/../../../outside",
])
def test_traversal_is_refused(root: Path, suffix: str) -> None:
    with pytest.raises(fsbrowse.BrowseError, match="outside"):
        fsbrowse.list_dir(f"{root}/{suffix}")


def test_paths_outside_the_roots_are_refused(root: Path) -> None:
    for raw in (str(root.parent / "outside"), str(root.parent), str(Path(root.anchor))):
        with pytest.raises(fsbrowse.BrowseError):
            fsbrowse.safe_resolve(raw)
    # A sibling whose name only starts with the root name is not inside it.
    sibling = root.parent / "root-other"
    sibling.mkdir()
    with pytest.raises(fsbrowse.BrowseError):
        fsbrowse.safe_resolve(str(sibling))


@pytest.mark.parametrize("raw", ["", "   ", "nas", "./nas", "bad\x00name"])
def test_empty_relative_and_nul_paths_are_refused(root: Path, raw: str) -> None:
    with pytest.raises(fsbrowse.BrowseError):
        fsbrowse.safe_resolve(raw)


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    # Windows without the symlink privilege: a directory junction is the same
    # kind of escape (a folder entry that resolves to another place).
    try:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, AttributeError, OSError):
        pytest.skip("This OS or user cannot create symlinks")


def test_symlink_escape_is_refused_and_hidden(root: Path) -> None:
    outside = root.parent / "outside"
    _symlink_or_skip(root / "nas" / "escape", outside)
    # Opening the link, or anything below it, is refused.
    with pytest.raises(fsbrowse.BrowseError, match="outside"):
        fsbrowse.list_dir(str(root / "nas" / "escape"))
    with pytest.raises(fsbrowse.BrowseError, match="outside"):
        fsbrowse.list_dir(str(root / "nas" / "escape" / "private"))
    with pytest.raises(fsbrowse.BrowseError):
        fsbrowse.make_dir(str(root / "nas" / "escape"), "new")
    assert not (outside / "new").exists()
    # The listing does not offer a link that cannot be opened.
    assert [d.label for d in fsbrowse.list_dir(str(root / "nas")).dirs] == ["git"]


def test_symlink_inside_the_root_is_allowed(root: Path) -> None:
    _symlink_or_skip(root / "shortcut", root / "nas" / "git")
    assert fsbrowse.safe_resolve(str(root / "shortcut")) == root / "nas" / "git"
    assert "shortcut" in [d.label for d in fsbrowse.list_dir(str(root)).dirs]


@pytest.mark.parametrize("name", ["", "..", ".", "a/b", "a\\b", "../x", "x\x00y", "n" * 300])
def test_new_folder_name_is_validated(root: Path, name: str) -> None:
    with pytest.raises(fsbrowse.BrowseError):
        fsbrowse.make_dir(str(root / "nas"), name)
    assert sorted(p.name for p in (root / "nas").iterdir()) == ["git", "notes.txt"]


def test_new_folder_is_made_once(root: Path) -> None:
    made = fsbrowse.make_dir(str(root / "nas"), "mirrors")
    assert made == root / "nas" / "mirrors" and made.is_dir()
    with pytest.raises(fsbrowse.BrowseError, match="already there"):
        fsbrowse.make_dir(str(root / "nas"), "mirrors")


def test_check_folder_reports_space_and_leaves_no_file(root: Path) -> None:
    folder = root / "nas" / "git"
    check = fsbrowse.check_folder(folder)
    assert check.writable and check.problem == ""
    assert check.total and check.free is not None and check.free <= check.total
    assert list(folder.iterdir()) == []


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="Needs POSIX permissions and a non-root user")
def test_check_folder_explains_a_read_only_folder(root: Path) -> None:
    folder = root / "nas" / "git"
    folder.chmod(0o555)
    try:
        check = fsbrowse.check_folder(folder)
    finally:
        folder.chmod(0o755)
    assert not check.writable
    assert "uid 1000" in check.problem


def test_prepare_folder_makes_a_missing_folder_inside_the_root(root: Path) -> None:
    path, check = fsbrowse.prepare_folder(str(root / "new" / "mirrors"))
    assert path.is_dir() and check.writable
    with pytest.raises(fsbrowse.BrowseError):
        fsbrowse.prepare_folder(str(root.parent / "outside" / "made"))
    assert not (root.parent / "outside" / "made").exists()
    with pytest.raises(fsbrowse.BrowseError, match="is a file"):
        fsbrowse.prepare_folder(str(root / "nas" / "notes.txt"))


def test_overlap_warning(root: Path) -> None:
    a, b = root / "nas", root / "nas" / "git"
    assert "same folder" in fsbrowse.overlap_warning(a, a)
    assert "inside the other" in fsbrowse.overlap_warning(a, b)
    assert "inside the other" in fsbrowse.overlap_warning(b, a)
    assert fsbrowse.overlap_warning(b, root / "other") == ""
    assert fsbrowse.overlap_warning(a, "") == ""
