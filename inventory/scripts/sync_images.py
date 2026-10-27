from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from inventory_util import SyncError, require


class ImagesMixin:
    def _auth_headers(self) -> dict[str, str]:
        scheme = "Bearer" if self.token.startswith("nbt_") else "Token"
        return {"Authorization": f"{scheme} {self.token}", "Accept": "application/json"}

    def _image_path(self, reference: Any) -> Path:
        if not reference:
            raise SyncError("empty image reference")
        raw = Path(str(reference))
        if raw.is_absolute():
            if not raw.is_file():
                raise SyncError(f"image file {raw} does not exist")
            return raw
        if self.images_dir is None:
            raise SyncError("no image directory configured")
        path = self.images_dir / raw.name
        if not path.is_file():
            raise SyncError(f"image file {path} does not exist")
        return path

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _download_headers(self, url: str) -> dict[str, str]:
        headers = {"Accept": "*/*"}
        # NetBox media is normally public. Only send the API token when the
        # image is on the NetBox origin; do not leak it to an external CDN.
        if urlsplit(url).netloc == urlsplit(self.url).netloc:
            headers["Authorization"] = self._auth_headers()["Authorization"]
        return headers

    def _remote_image_matches(self, value: Any, path: Path) -> bool:
        if not value or not self.url:
            return False
        url = urljoin(f"{self.url}/", str(value))
        try:
            response = requests.get(
                url,
                headers=self._download_headers(url),
                timeout=60,
            )
        except requests.RequestException:
            # A missing or unreachable remote image should be repaired rather
            # than silently treated as unchanged.
            return False
        if response.status_code >= 300:
            return False
        return hashlib.sha256(response.content).hexdigest() == self._file_digest(path)

    def _patch_device_type_image(self, obj: Any, field: str, path: Path) -> None:
        current = getattr(obj, field, None)
        # NetBox stores uploads under a generated filename, so compare the
        # actual image content instead of the remote basename.
        if not self.force_images and current and self._remote_image_matches(current, path):
            return
        if not isinstance(getattr(obj, "id", None), int) or obj.id < 0:
            # The device type is created earlier in this dry run; its mock ID
            # cannot be used for an upload URL.
            self.counts["unchanged"] += 1
            return
        if not self.url or not self.token:
            raise SyncError("NetBox URL/token are required to upload device-type images")
        self.log("update", "device_type", f"{getattr(obj, 'slug', obj.id)} {field}", path.name)
        if self.dry_run:
            return
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as handle:
            response = requests.patch(
                f"{self.url}/api/dcim/device-types/{obj.id}/",
                headers=self._auth_headers(),
                files={field: (path.name, handle, mime)},
                timeout=60,
            )
        if response.status_code >= 300:
            raise SyncError(
                f"upload {field} for device type {getattr(obj, 'slug', obj.id)}: "
                f"HTTP {response.status_code}: {response.text[:300]}"
            )

    def sync_device_type_images(self, obj: Any, item: dict[str, Any]) -> None:
        images = item.get("images") or {}
        catalog_images = item.get("_catalog_images") or {}
        for field in ("front", "rear"):
            reference = images.get(field) or catalog_images.get(field)
            if reference:
                self._patch_device_type_image(
                    obj, f"{field}_image", self._image_path(reference)
                )

        desired_other = images.get("other") or []
        desired_names: set[str] = set()
        existing = []
        if self.url and self.token:
            existing = list(
                self.nb.extras.image_attachments.filter(
                    object_type="dcim.devicetype", object_id=obj.id
                )
            )
        by_name = {str(getattr(att, "name", "")): att for att in existing}

        for entry in desired_other:
            if not isinstance(entry, dict):
                raise SyncError(f"device type {getattr(obj, 'slug', obj.id)}: image must be a mapping")
            name = str(require(entry, "name", "device type image"))
            path = self._image_path(entry.get("file"))
            desired_names.add(name)
            attachment = by_name.get(name)
            # NetBox renames uploaded files; compare content instead of names.
            if (
                attachment
                and not self.force_images
                and self._remote_image_matches(getattr(attachment, "image", None), path)
            ):
                continue
            if not self.url or not self.token:
                raise SyncError("NetBox URL/token are required to upload device-type images")
            if not isinstance(getattr(obj, "id", None), int) or obj.id < 0:
                self.counts["unchanged"] += 1
                continue
            self.log(
                "update" if attachment else "create",
                "image_attachment",
                f"{getattr(obj, 'slug', obj.id)}/{name}",
                path.name,
            )
            if self.dry_run:
                continue
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            description = f"GitOps: {entry.get('description', '')}".strip()
            with path.open("rb") as handle:
                if attachment:
                    response = requests.patch(
                        f"{self.url}/api/extras/image-attachments/{attachment.id}/",
                        headers=self._auth_headers(),
                        data={
                            "object_type": "dcim.devicetype",
                            "object_id": obj.id,
                            "name": name,
                            "description": description,
                        },
                        files={"image": (path.name, handle, mime)},
                        timeout=60,
                    )
                else:
                    response = requests.post(
                        f"{self.url}/api/extras/image-attachments/",
                        headers=self._auth_headers(),
                        data={
                            "object_type": "dcim.devicetype",
                            "object_id": obj.id,
                            "name": name,
                            "description": description,
                        },
                        files={"image": (path.name, handle, mime)},
                        timeout=60,
                    )
            if response.status_code >= 300:
                raise SyncError(
                    f"upload image {name!r} for device type {getattr(obj, 'slug', obj.id)}: "
                    f"HTTP {response.status_code}: {response.text[:300]}"
                )

        preserve_catalog_images = bool(item.get("_dtl_failed"))
        for attachment in existing:
            name = str(getattr(attachment, "name", ""))
            if name in desired_names:
                continue
            if preserve_catalog_images and name.startswith("DTL "):
                # The catalog was unavailable this run; keep the previously
                # uploaded elevation images instead of deleting them.
                continue
            if not str(getattr(attachment, "description", "") or "").startswith("GitOps:"):
                continue
            if not self.url or not self.token:
                continue
            self.log("delete", "image_attachment", f"{getattr(obj, 'slug', obj.id)}/{name}")
            if self.dry_run:
                continue
            response = requests.delete(
                f"{self.url}/api/extras/image-attachments/{attachment.id}/",
                headers=self._auth_headers(),
                timeout=60,
            )
            if response.status_code >= 300:
                raise SyncError(
                    f"delete image {name!r} for device type {getattr(obj, 'slug', obj.id)}: "
                    f"HTTP {response.status_code}: {response.text[:300]}"
                )
