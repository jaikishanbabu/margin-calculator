"""The same contract runs against every storage backend (Google Drive gets added to BACKENDS in Stage 2)."""
import pytest

from app.storage import AlreadyExists, ArchiveStore, NotFound, StorageError
from app.storage.local import LocalStore
from app.storage.supabase import SupabaseStore
from fake_supabase import KEY, FakeStorage


@pytest.fixture(params=["local", "supabase"])
def store(request, tmp_path):
    if request.param == "local":
        yield LocalStore(tmp_path / "s")
    else:
        fake = FakeStorage()
        yield SupabaseStore(fake.url, KEY, "margin-archive")
        fake.close()


def test_put_get_roundtrip(store):
    obj = store.put_verified("originals/NFO/20261104/span/abc_file.zip", b"hello bytes")
    assert obj.size == 11 and len(obj.sha256) == 64
    assert store.get("originals/NFO/20261104/span/abc_file.zip") == b"hello bytes"
    assert store.exists("originals/NFO/20261104/span/abc_file.zip")
    assert not store.exists("originals/nope")


def test_write_once(store):
    store.put("a/b.txt", b"1")
    with pytest.raises(AlreadyExists):
        store.put("a/b.txt", b"2")
    assert store.get("a/b.txt") == b"1"


def test_missing_raises_notfound(store):
    with pytest.raises(NotFound):
        store.get("does/not/exist")


def test_list_recursive_and_delete(store):
    for k in ["backups/20261101T000000Z/db.jsonl.gz", "backups/20261101T000000Z/manifest.json",
              "backups/20261108T000000Z/manifest.json", "originals/x/y.zip"]:
        store.put(k, b"x")
    assert store.list("backups/") == ["backups/20261101T000000Z/db.jsonl.gz", "backups/20261101T000000Z/manifest.json",
                                      "backups/20261108T000000Z/manifest.json"]
    store.delete("backups/20261101T000000Z/db.jsonl.gz")
    assert "backups/20261101T000000Z/db.jsonl.gz" not in store.list("backups/")


@pytest.mark.parametrize("key", ["/abs", "../up", "a/../b", "a\\b", ""])
def test_unsafe_keys_rejected(store, key):
    with pytest.raises(StorageError):
        store.put(key, b"x")


def test_put_verified_detects_corruption(tmp_path):
    class Corrupting(LocalStore):
        def get(self, key):
            return super().get(key) + b"!"
    with pytest.raises(StorageError, match="Checksum mismatch"):
        Corrupting(tmp_path / "c").put_verified("k/v", b"data")


def test_supabase_wrong_key_fails_loudly():
    fake = FakeStorage()
    try:
        with pytest.raises(StorageError):
            SupabaseStore(fake.url, "wrong-key", "margin-archive").put("a/b", b"x")
    finally:
        fake.close()


def test_interface_has_no_backend_specific_methods():
    public = {m for m in dir(ArchiveStore) if not m.startswith("_")}
    # sign_* / copy have working defaults in the base class (None = "can't sign" -> callers fall back to
    # sending bytes through the engine), so a new backend still only has to implement the five basics.
    assert public == {"put", "get", "exists", "list", "delete", "put_verified", "name",
                      "sign_download", "sign_upload", "copy"}


def test_copy_is_write_once_on_every_backend(tmp_path):
    fake = FakeStorage()
    try:
        for store in (LocalStore(tmp_path / "l"), SupabaseStore(fake.url, KEY, "margin-archive")):
            store.put("in/a", b"hello")
            store.copy("in/a", "out/a")
            assert store.get("out/a") == b"hello"
            with pytest.raises(AlreadyExists):
                store.copy("in/a", "out/a")
    finally:
        fake.close()


def test_supabase_signed_links_round_trip():
    import urllib.request
    fake = FakeStorage()
    try:
        store = SupabaseStore(fake.url, KEY, "margin-archive")
        up = store.sign_upload("incoming/1/abc_file.zip")
        assert up.startswith(fake.url + "/storage/v1/object/upload/sign/")
        req = urllib.request.Request(up, data=b"payload", method="PUT")     # no Authorization header
        assert urllib.request.urlopen(req).status == 200
        assert store.get("incoming/1/abc_file.zip") == b"payload"
        down = store.sign_download("incoming/1/abc_file.zip")
        assert urllib.request.urlopen(down).read() == b"payload"
        assert LocalStore.sign_download(None, "x") is None      # local disk can't sign -> engine path
    finally:
        fake.close()
