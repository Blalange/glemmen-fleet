from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

import requests

from inventory_util import SyncError, require


class ImagesMixin:
    def _auth_headers(self) -> dict[str, str]:
        scheme = "Bearer" if self.token.startswith("nbt_") else "Token"
        return {"Authorization": f"{scheme} {self.token}", "Accept": "application/json"}

    def _image_path(self, reference: Any) -> Path:
        if not reference:
            raise SyncError("empty image reference")
        if self.images_dir is None:
            raise SyncError("no image directory configured")
        path = self.images_dir / Path(str(reference)).name
        if not path.is_file():
            raise SyncError(f"image file {path} does not exist")
        return path

    @staticmethod
    def _remote_basename(value: Any) -> str:
        if not value:
            return ""
        return str(value).rstrip("/").split("/")[-1]

    def _patch_device_type_image(self, obj: Any, field: str, path: Path) -> None:
        current = getattr(obj, field, None)
        # NetBox stores uploads under a generated filename, so the remote
        # basename cannot be compared with the source file. Upload once and
        # require --force-images to replace an existing image.
        if not self.force_images and current:
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
        for field in ("front", "rear"):
            if images.get(field):
                self._patch_device_type_image(obj, f"{field}_image", self._image_path(images[field]))

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
            # NetBox renames uploaded files; do not compare basenames. Replace
            # an existing attachment only when --force-images is used.
            if attachment and not self.force_images:
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

        for attachment in existing:
            name = str(getattr(attachment, "name", ""))
            if name in desired_names:
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
