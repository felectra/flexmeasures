"""Seed the FlexMeasures asset tree for the Mykolaiv office pilot.

Idempotent — safe to re-run. Implements the model documented in bench-assets.md.
Under the Felectra account it creates:

    Office (building)          site / grid connection point (grid exchange from deye/ac/*)
    └── BatteryBank (battery)         the battery as the inverter reports it (deye/battery/*)
        ├── String A (battery) 14S3P, own JK-BMS   — placeholder, no telemetry yet
        └── String B (battery) 14S3P, own JK-BMS   — placeholder, no telemetry yet
    Generator (process)        sibling of Office (via ATS) — placeholder, no signal

Monitoring is per-string (two independent JK-BMS with divergent cycle counts), but control
is at the bank level, because the inverter sees one pair of terminals and cannot address the
strings — modelling them as one asset would bake in an assumption the hardware contradicts.
The Generator is a sibling of Office, not a child, because it feeds the site through an ATS.

Sensors and the storage flex-model are a later step; wiring the deye/* and jkbms/* data in is
the job of the ingestion side (a separate repo — see bench-assets.md and ../deye-imex).

Run inside the server container, from the repository root on the board:

    podman exec -i rock3a_server_1 python - < deploy/rock3a/seed-assets.py
"""

from flexmeasures.app import create as create_app

app = create_app()
with app.app_context():
    from flexmeasures.data import db
    from flexmeasures.data.models.generic_assets import GenericAsset, GenericAssetType
    from flexmeasures.data.models.user import Account

    ACCOUNT_NAME = "Felectra"
    account = db.session.query(Account).filter_by(name=ACCOUNT_NAME).one()

    def asset_type(name):
        return db.session.query(GenericAssetType).filter_by(name=name).one()

    def get_or_create(
        name, type_name, parent=None, latitude=None, longitude=None, description=None
    ):
        query = db.session.query(GenericAsset).filter_by(
            name=name,
            account_id=account.id,
            parent_asset_id=parent.id if parent is not None else None,
        )
        existing = query.first()
        if existing is not None:
            return existing, False
        asset = GenericAsset(
            name=name,
            generic_asset_type=asset_type(type_name),
            owner=account,
            parent_asset=parent,
            latitude=latitude,
            longitude=longitude,
            description=description,
        )
        db.session.add(asset)
        db.session.flush()
        return asset, True

    # Office is the only root that carries a location, so the dashboard map centres on Mykolaiv.
    office, c_office = get_or_create(
        "Office",
        "building",
        latitude=46.975,
        longitude=31.995,
        description="Mykolaiv lab — site and grid connection point (grid exchange from deye/ac/*).",
    )
    bank, c_bank = get_or_create(
        "BatteryBank",
        "battery",
        parent=office,
        description="The battery as the inverter reports it (deye/battery/*); it cannot see the two strings.",
    )
    string_a, c_a = get_or_create(
        "String A",
        "battery",
        parent=bank,
        description="14S3P, own JK-BMS. PLACEHOLDER — per-string BLE telemetry not deployed yet (see bench-assets.md).",
    )
    string_b, c_b = get_or_create(
        "String B",
        "battery",
        parent=bank,
        description="14S3P, own JK-BMS. PLACEHOLDER — per-string BLE telemetry not deployed yet (see bench-assets.md).",
    )
    # Generator is a sibling of Office (a root asset), not a child: it feeds the site via an ATS.
    generator, c_gen = get_or_create(
        "Generator",
        "process",
        description="7 kW via ATS. PLACEHOLDER — no running signal anywhere, so nothing can be scheduled around it.",
    )

    db.session.commit()

    def show(asset, created):
        tag = "created" if created else "exists "
        parent = (
            f"  parent={asset.parent_asset.name}"
            if asset.parent_asset is not None
            else "  (root)"
        )
        print(
            f"  [{tag}] id={asset.id:<4} {asset.name:<10} <{asset.generic_asset_type.name}>{parent}"
        )

    print(f"Asset tree under account '{account.name}' (id={account.id}):")
    for asset, created in [
        (office, c_office),
        (bank, c_bank),
        (string_a, c_a),
        (string_b, c_b),
        (generator, c_gen),
    ]:
        show(asset, created)
