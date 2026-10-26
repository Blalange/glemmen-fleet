from __future__ import annotations

from pathlib import Path
from typing import Any

from dtl_catalog import DtlCatalog
from sync_catalog import enrich_inventory


class FakeCatalog:
    def __init__(
        self,
        definitions: dict[str, dict[str, Any]],
        images: dict[tuple[str, str], Path] | None = None,
    ) -> None:
        self.definitions = definitions
        self.images = images or {}
        self.image_requests: list[tuple[str, str]] = []

    def resolve(self, item: dict[str, Any], *, kind: str, manufacturer_name: str = "") -> Any:
        return self.definitions.get(str(item.get("slug")))

    def image_path(self, definition: dict[str, Any], face: str) -> Path | None:
        self.image_requests.append((str(definition.get("slug")), face))
        return self.images.get((str(definition.get("slug")), face))


def test_dtl_enriches_manual_device_type() -> None:
    inventory = {
        "device_types": [
            {
                "manufacturer": "ubiquiti",
                "model": "U6 Pro",
                "slug": "ubiquiti-u6-pro",
                "dtl": "device-types/Ubiquiti/U6-Pro.yaml",
                "u_height": 0,
                "description": "Manual description.",
                "interfaces": [
                    {"name": "Port 1", "type": "1000base-t", "description": "Manual port."}
                ],
            }
        ],
        "module_types": [],
    }
    definitions = {
        "ubiquiti-u6-pro": {
            "comments": "Catalog comment.\n",
            "part_number": "U6-Pro",
            "description": "Catalog description.",
            "interfaces": [
                {"name": "Port 1", "type": "1000base-t", "poe_mode": "pd"},
                {"name": "Wlan 0", "type": "ieee802.11n"},
            ],
            "power-ports": [{"name": "Input", "type": "iec-60320-c14"}],
        }
    }
    report = enrich_inventory(inventory, FakeCatalog(definitions))  # type: ignore[arg-type]
    device_type = inventory["device_types"][0]
    assert report.device_types_enriched == 1
    assert report.components_added == 2
    assert device_type["description"] == "Manual description."
    assert device_type["comments"] == "Catalog comment."
    assert device_type["part_number"] == "U6-Pro"
    assert device_type["interfaces"][0]["description"] == "Manual port."
    assert device_type["interfaces"][0]["poe_mode"] == "pd"
    assert device_type["interfaces"][1]["name"] == "Wlan 0"
    assert device_type["power_ports"] == [{"name": "Input", "type": "iec-60320-c14"}]


def test_dtl_enriches_module_type_without_device_bays() -> None:
    inventory = {
        "device_types": [],
        "module_types": [
            {
                "manufacturer": "intel",
                "model": "E610-XT2",
                "slug": "intel-e610-xt2",
                "dtl": "module-types/Intel/E610-XT2.yaml",
                "profile": "Expansion card",
            }
        ],
    }
    definitions = {
        "intel-e610-xt2": {
            "interfaces": [{"name": "Ethernet/{module}/1", "type": "10gbase-t"}],
            "device-bays": [{"name": "Not valid on a module"}],
        }
    }
    report = enrich_inventory(inventory, FakeCatalog(definitions))  # type: ignore[arg-type]
    module_type = inventory["module_types"][0]
    assert report.module_types_enriched == 1
    assert module_type["interfaces"] == [{"name": "Ethernet/{module}/1", "type": "10gbase-t"}]
    assert "device_bays" not in module_type


def test_dtl_component_without_required_type_is_skipped() -> None:
    inventory = {
        "device_types": [{"manufacturer": "dell", "model": "R250", "slug": "dell-poweredge-r250"}],
        "module_types": [],
    }
    definitions = {
        "dell-poweredge-r250": {
            "interfaces": [{"name": "No type"}],
            "power-ports": [{"name": "Power 1"}],
        }
    }
    report = enrich_inventory(inventory, FakeCatalog(definitions))  # type: ignore[arg-type]
    device_type = inventory["device_types"][0]
    assert report.components_added == 1
    assert "interfaces" not in device_type
    assert device_type["power_ports"] == [{"name": "Power 1"}]


def test_manual_images_win_over_dtl_images(tmp_path: Path) -> None:
    inventory = {
        "device_types": [
            {
                "manufacturer": "dell",
                "model": "PowerEdge R630",
                "slug": "dell-poweredge-r630",
                "images": {
                    "front": "images/r630-front.jpg",
                    "rear": "images/r630-rear.jpg",
                },
            }
        ],
        "module_types": [],
    }
    definitions = {
        "dell-poweredge-r630": {
            "slug": "dell-poweredge-r630",
            "front_image": True,
            "rear_image": True,
        },
    }
    catalog = FakeCatalog(
        definitions,
        images={
            ("dell-poweredge-r630", "front"): tmp_path / "front.png",
            ("dell-poweredge-r630", "rear"): tmp_path / "rear.png",
        },
    )
    report = enrich_inventory(inventory, catalog)  # type: ignore[arg-type]
    device_type = inventory["device_types"][0]
    assert report.images_added == 2
    assert "_catalog_images" not in device_type
    other = device_type["images"]["other"]
    assert [entry["name"] for entry in other] == [
        "DTL front elevation",
        "DTL rear elevation",
    ]
    assert catalog.image_requests == [
        ("dell-poweredge-r630", "front"),
        ("dell-poweredge-r630", "rear"),
    ]


def test_dtl_images_fill_missing_manual_faces(tmp_path: Path) -> None:
    inventory = {
        "device_types": [
            {
                "manufacturer": "dell",
                "model": "PowerEdge R630",
                "slug": "dell-poweredge-r630",
            }
        ],
        "module_types": [],
    }
    definitions = {
        "dell-poweredge-r630": {
            "slug": "dell-poweredge-r630",
            "front_image": True,
            "rear_image": True,
        },
    }
    catalog = FakeCatalog(
        definitions,
        images={
            ("dell-poweredge-r630", "front"): tmp_path / "front.png",
            ("dell-poweredge-r630", "rear"): tmp_path / "rear.png",
        },
    )
    report = enrich_inventory(inventory, catalog)  # type: ignore[arg-type]
    assert report.images_added == 2
    assert inventory["device_types"][0]["_catalog_images"] == {
        "front": str(tmp_path / "front.png"),
        "rear": str(tmp_path / "rear.png"),
    }


def test_dtl_image_download_caches_bytes(tmp_path: Path) -> None:
    class FakeResponse:
        status_code = 200
        content = b"PNG"

    catalog = DtlCatalog(cache_dir=tmp_path)
    catalog.session.get = lambda *args, **kwargs: FakeResponse()  # type: ignore[method-assign]
    path = catalog.image_path(
        {"front_image": True, "manufacturer": "Dell", "slug": "dell-poweredge-r630"},
        "front",
    )
    assert path is not None
    assert path.is_file()
    assert path.read_bytes() == b"PNG"
