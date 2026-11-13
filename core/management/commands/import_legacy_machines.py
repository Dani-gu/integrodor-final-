"""Import machine records from a legacy MariaDB dump without executing SQL."""

import hashlib
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.management.commands.import_legacy_mariadb import _insert_statements
from core.models import AuditLog, Machine


STATUS_MAP = {
    "operativa": Machine.Status.OPERATIONAL,
    "en_mantenimiento": Machine.Status.MAINTENANCE,
    "fuera_de_servicio": Machine.Status.OUT_OF_SERVICE,
}


class Command(BaseCommand):
    help = "Importa únicamente las máquinas de un volcado SQL anterior de Matriplast."

    def add_arguments(self, parser):
        parser.add_argument("sql_file", help="Ruta al archivo SQL de MariaDB/phpMyAdmin.")

    @transaction.atomic
    def handle(self, *args, **options):
        path = Path(options["sql_file"])
        if not path.is_file():
            raise CommandError("No se encontró el archivo SQL indicado.")
        if path.stat().st_size > 100 * 1024 * 1024:
            raise CommandError("El archivo supera el límite de 100 MB.")
        raw = path.read_bytes()
        file_hash = hashlib.sha256(raw).hexdigest()
        if AuditLog.objects.filter(entity="LegacyMachinesImport", entity_id=file_hash[:32]).exists():
            raise CommandError("Las máquinas de este archivo ya fueron importadas.")
        try:
            sql_text = raw.decode("utf-8-sig")
            rows = [row for table, table_rows in _insert_statements(sql_text) if table == "maquinas" for row in table_rows]
        except (UnicodeDecodeError, ValueError) as exc:
            raise CommandError("No se pudo interpretar el volcado SQL.") from exc
        if not rows:
            raise CommandError("No se encontraron registros de máquinas en el archivo.")
        actor = get_user_model().objects.filter(is_active=True).order_by("-is_superuser", "id").first()
        if actor is None:
            raise CommandError("Primero crea un usuario activo en el sistema nuevo.")

        created = 0
        existing = 0
        for row in rows:
            name = str(row.get("nombre") or "").strip()
            if not name or len(name) > 140:
                raise CommandError("El volcado contiene un nombre de máquina inválido.")
            raw_status = str(row.get("estado") or "").strip().casefold()
            status = STATUS_MAP.get(raw_status, Machine.Status.MAINTENANCE)
            machine, was_created = Machine.objects.get_or_create(name=name, defaults={"status": status})
            if was_created:
                created += 1
                AuditLog.objects.create(
                    actor=actor,
                    action="IMPORT",
                    entity="Machine",
                    entity_id=machine.pk,
                    details={"legacy_id": str(row.get("id", "")), "status": machine.status},
                )
            else:
                existing += 1

        AuditLog.objects.create(
            actor=actor,
            action="IMPORT",
            entity="LegacyMachinesImport",
            entity_id=file_hash[:32],
            details={"created": created, "already_present": existing},
        )
        self.stdout.write(self.style.SUCCESS(f"Máquinas importadas: {created} nuevas; {existing} ya existentes."))
        self.stdout.write(self.style.WARNING("Los estados provienen del archivo anterior. Confírmalos antes de asignar una máquina a un lote."))
