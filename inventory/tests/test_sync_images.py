from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import sync_images
from sync_context import SyncContext
from sync_images import ImagesMixin


class ImageSync(ImagesMixin, SyncContext):
    pass


class FakeResponse:
    def __init__(self, content: bytes = b"", status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code


class Attachments:
    def __init__(self, items: list[SimpleNamespace]) -> None:
        self.items = items

    def filter(self, **kwargs: object) -> list[SimpleNamespace]:
        return list(self.items)


def make_sync(
    tmp_path: Path,
    *,
    attachments: list[SimpleNamespace] | None = None,
    force_images: bool = False,
) -> ImageSync:
    nb = SimpleNamespace(
        extras=SimpleNamespace(
            image_attachments=Attachments(attachments or []),
        )
    )
    return ImageSync(
        nb,
        {},
        url="http://netbox.example",
        token="test-token",
        images_dir=tmp_path,
        force_images=force_images,
    )


def patch_requests(
    monkeypatch: pytest.MonkeyPatch,
    remote_content: bytes = b"remote",
    *,
    remote_status: int = 200,
) -> dict[str, list[tuple[str, dict[str, object]]]]:
    calls: dict[str, list[tuple[str, dict[str, object]]]] = {
        "get": [],
        "patch": [],
        "post": [],
    }

    def get(url: str, **kwargs: object) -> FakeResponse:
        calls["get"].append((url, kwargs))
        return FakeResponse(remote_content, remote_status)

    def patch(url: str, **kwargs: object) -> FakeResponse:
        calls["patch"].append((url, kwargs))
        return FakeResponse()

    def post(url: str, **kwargs: object) -> FakeResponse:
        calls["post"].append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(sync_images.requests, "get", get)
    monkeypatch.setattr(sync_images.requests, "patch", patch)
    monkeypatch.setattr(sync_images.requests, "post", post)
    return calls


def test_changed_front_image_is_uploaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    image = tmp_path / "front.jpg"
    image.write_bytes(b"new image")
    calls = patch_requests(monkeypatch, b"old image")
    obj = SimpleNamespace(
        id=1,
        slug="server",
        front_image="http://netbox.example/media/old-name.jpg",
    )

    make_sync(tmp_path).sync_device_type_images(
        obj, {"images": {"front": "images/front.jpg"}}
    )

    assert calls["get"][0][0] == "http://netbox.example/media/old-name.jpg"
    assert len(calls["patch"]) == 1
    assert calls["patch"][0][0] == "http://netbox.example/api/dcim/device-types/1/"
    assert "front_image" in calls["patch"][0][1]["files"]


def test_unchanged_front_image_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = tmp_path / "front.jpg"
    image.write_bytes(b"same image")
    calls = patch_requests(monkeypatch, b"same image")
    obj = SimpleNamespace(
        id=1,
        slug="server",
        front_image="/media/front.jpg",
    )

    make_sync(tmp_path).sync_device_type_images(
        obj, {"images": {"front": "images/front.jpg"}}
    )

    assert calls["get"][0][0] == "http://netbox.example/media/front.jpg"
    assert calls["patch"] == []


def test_force_images_uploads_unchanged_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = tmp_path / "front.jpg"
    image.write_bytes(b"same image")
    calls = patch_requests(monkeypatch, b"same image")
    obj = SimpleNamespace(id=1, slug="server", front_image="/media/front.jpg")

    make_sync(tmp_path, force_images=True).sync_device_type_images(
        obj, {"images": {"front": "images/front.jpg"}}
    )

    assert len(calls["patch"]) == 1


def test_missing_remote_image_is_uploaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = tmp_path / "front.jpg"
    image.write_bytes(b"image")
    calls = patch_requests(monkeypatch, b"", remote_status=404)
    obj = SimpleNamespace(id=1, slug="server", front_image="/media/missing.jpg")

    make_sync(tmp_path).sync_device_type_images(
        obj, {"images": {"front": "images/front.jpg"}}
    )

    assert len(calls["patch"]) == 1


def test_changed_attachment_is_updated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = tmp_path / "other.jpg"
    image.write_bytes(b"new image")
    calls = patch_requests(monkeypatch, b"old image")
    attachment = SimpleNamespace(
        id=7,
        name="Front",
        description="GitOps: old",
        image="http://netbox.example/media/attachment.jpg",
    )
    obj = SimpleNamespace(id=1, slug="server")

    make_sync(tmp_path, attachments=[attachment]).sync_device_type_images(
        obj,
        {
            "images": {
                "other": [
                    {
                        "name": "Front",
                        "file": "images/other.jpg",
                        "description": "new",
                    }
                ]
            }
        },
    )

    assert len(calls["patch"]) == 1
    assert calls["patch"][0][0] == "http://netbox.example/api/extras/image-attachments/7/"
    assert "image" in calls["patch"][0][1]["files"]


def test_unchanged_attachment_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = tmp_path / "other.jpg"
    image.write_bytes(b"same image")
    calls = patch_requests(monkeypatch, b"same image")
    attachment = SimpleNamespace(
        id=7,
        name="Front",
        description="GitOps: old",
        image="/media/attachment.jpg",
    )
    obj = SimpleNamespace(id=1, slug="server")

    make_sync(tmp_path, attachments=[attachment]).sync_device_type_images(
        obj,
        {
            "images": {
                "other": [
                    {
                        "name": "Front",
                        "file": "images/other.jpg",
                        "description": "new",
                    }
                ]
            }
        },
    )

    assert calls["patch"] == []
    assert calls["post"] == []


def test_external_image_fetch_does_not_send_netbox_token(tmp_path: Path) -> None:
    sync = make_sync(tmp_path)

    external = sync._download_headers("https://cdn.example/media/front.jpg")
    internal = sync._download_headers("http://netbox.example/media/front.jpg")

    assert "Authorization" not in external
    assert internal["Authorization"].endswith("test-token")
