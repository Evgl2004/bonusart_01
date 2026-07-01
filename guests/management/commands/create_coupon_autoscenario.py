from __future__ import annotations

from typing import Iterable

from django.core.management.base import BaseCommand, CommandError

from guests.forms import (
    COUPON_TEMPLATE_MODE_CREATE,
    COUPON_TEMPLATE_MODE_EXISTING,
    CouponAutomationScenarioCreateForm,
)
from guests.models import BotProfile, CouponAutomationConfig, MessageTemplate


class Command(BaseCommand):
    """
    Создаёт пользовательский купонный автосценарий тем же путём, что и UI-мастер.

    Без флага --confirm команда только валидирует входные данные и показывает,
    какие связанные сущности будут созданы. Это защищает от случайного появления
    неполного автосценария в базе.
    """

    help = (
        "Создаёт купонный автосценарий из типовой основы или на основе существующего "
        "автосценария. По умолчанию выполняет только сухой прогон."
    )

    def add_arguments(self, parser):
        parser.add_argument("--code", required=True, help="Новый код автосценария.")
        parser.add_argument("--name", required=True, help="Название автосценария.")
        parser.add_argument(
            "--scenario-type",
            choices=[value for value, _label in CouponAutomationConfig.ScenarioType.choices],
            default=None,
            help="Типовая основа автосценария.",
        )
        parser.add_argument(
            "--source-config-id",
            type=int,
            default=None,
            help="ID существующей купонной настройки, из которой нужно скопировать безопасные параметры и правила.",
        )
        parser.add_argument(
            "--inactive-days",
            type=int,
            default=None,
            help="Порог неактивности для типовой основы «Гость не был N дней + купон».",
        )
        parser.add_argument(
            "--birthday-preparation-window-days",
            type=int,
            default=None,
            help="Окно подготовки ко дню рождения для типовой основы «День рождения + купон».",
        )
        parser.add_argument(
            "--template-id",
            type=int,
            default=None,
            help="ID существующего активного шаблона сообщения.",
        )
        parser.add_argument("--template-name", default="", help="Название нового шаблона сообщения.")
        parser.add_argument("--template-description", default="", help="Описание нового шаблона сообщения.")
        parser.add_argument(
            "--template-text",
            default="",
            help="Текст нового шаблона сообщения. Для купонного автосценария нужен {coupon_code}.",
        )
        parser.add_argument(
            "--bot-profile-id",
            type=int,
            action="append",
            dest="bot_profile_ids",
            default=None,
            help="ID разрешённого бота. Можно передать несколько раз.",
        )
        parser.add_argument(
            "--bot-profile-code",
            action="append",
            dest="bot_profile_codes",
            default=None,
            help="Код разрешённого бота. Можно передать несколько раз.",
        )
        parser.add_argument(
            "--all-active-bots",
            action="store_true",
            help="Использовать все активные профили ботов.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Подтвердить создание `NotificationScenario`, `CouponAutomationConfig` и связанных записей.",
        )

    def handle(self, *args, **options):
        source_config = self._load_source_config(options.get("source_config_id"))
        form_data = self._build_form_data(options=options, source_config=source_config)
        form = CouponAutomationScenarioCreateForm(data=form_data, source_config=source_config)

        if not form.is_valid():
            self._print_form_errors(form)
            raise CommandError("Купонный автосценарий не создан: входные данные не прошли проверку.")

        self._print_validated_plan(form=form, source_config=source_config, confirmed=bool(options["confirm"]))
        if not bool(options["confirm"]):
            self.stdout.write("")
            self.stdout.write(
                "Режим: сухой прогон. База не изменена. "
                "Для создания автосценария повторите команду с --confirm."
            )
            return

        config = form.save()
        scenario = config.scenario
        self.stdout.write("")
        self.stdout.write("=== Созданный купонный автосценарий ===")
        self.stdout.write(f"scenario_id={scenario.id}")
        self.stdout.write(f"config_id={config.id}")
        self.stdout.write(f"scenario_code={scenario.code}")
        self.stdout.write(f"execution_mode={config.get_execution_mode_display()}")
        self.stdout.write("Сценарий создан выключенным; перед запуском откройте настройки и пульт управления.")

    def _load_source_config(self, source_config_id: int | None) -> CouponAutomationConfig | None:
        if source_config_id is None:
            return None

        source_config = (
            CouponAutomationConfig.objects.select_related("scenario", "scenario__template")
            .prefetch_related("scenario__bot_profiles", "coupon_rules")
            .filter(pk=source_config_id)
            .first()
        )
        if source_config is None:
            raise CommandError(f"Купонная настройка-основа не найдена: id={source_config_id}.")
        return source_config

    def _build_form_data(
        self,
        *,
        options: dict,
        source_config: CouponAutomationConfig | None,
    ) -> dict[str, object]:
        data: dict[str, object] = {}
        if source_config is not None:
            data.update(CouponAutomationScenarioCreateForm._build_initial_from_source(source_config))

        data["code"] = options["code"]
        data["name"] = options["name"]
        data["scenario_type"] = (
            options.get("scenario_type")
            or data.get("scenario_type")
            or CouponAutomationConfig.ScenarioType.INACTIVE_DAYS_COUPON
        )
        if options.get("inactive_days") is not None:
            data["inactive_days"] = options["inactive_days"]
        else:
            data.setdefault("inactive_days", 30)
        if options.get("birthday_preparation_window_days") is not None:
            data["birthday_preparation_window_days"] = options["birthday_preparation_window_days"]
        else:
            data.setdefault("birthday_preparation_window_days", 7)

        template_id = options.get("template_id")
        if template_id is not None:
            if not MessageTemplate.objects.filter(pk=template_id, is_active=True).exists():
                raise CommandError(f"Активный шаблон сообщения не найден: id={template_id}.")
            data["template_mode"] = COUPON_TEMPLATE_MODE_EXISTING
            data["existing_template"] = template_id
        else:
            data["template_mode"] = COUPON_TEMPLATE_MODE_CREATE
            if options.get("template_name"):
                data["template_name"] = options["template_name"]
            if options.get("template_description"):
                data["template_description"] = options["template_description"]
            if options.get("template_text"):
                data["template_text"] = options["template_text"]

        bot_profile_ids = self._resolve_bot_profile_ids(
            explicit_ids=options.get("bot_profile_ids") or [],
            explicit_codes=options.get("bot_profile_codes") or [],
            all_active=bool(options.get("all_active_bots")),
        )
        if bot_profile_ids:
            data["notification_bot_profiles"] = [str(value) for value in bot_profile_ids]
        elif "notification_bot_profiles" in data:
            data["notification_bot_profiles"] = [str(value) for value in data["notification_bot_profiles"]]

        return data

    def _resolve_bot_profile_ids(
        self,
        *,
        explicit_ids: Iterable[int],
        explicit_codes: Iterable[str],
        all_active: bool,
    ) -> list[int]:
        ids: list[int] = []
        for raw_id in explicit_ids:
            if raw_id not in ids:
                ids.append(int(raw_id))

        for raw_code in explicit_codes:
            code = str(raw_code or "").strip()
            if not code:
                continue
            bot = BotProfile.objects.filter(code=code, is_active=True).first()
            if bot is None:
                raise CommandError(f"Активный бот с кодом `{code}` не найден.")
            if bot.id not in ids:
                ids.append(int(bot.id))

        if all_active:
            for bot_id in BotProfile.objects.filter(is_active=True).order_by("id").values_list("id", flat=True):
                if int(bot_id) not in ids:
                    ids.append(int(bot_id))

        missing_ids = [
            bot_id
            for bot_id in ids
            if not BotProfile.objects.filter(pk=bot_id, is_active=True).exists()
        ]
        if missing_ids:
            raise CommandError(
                "Активные боты не найдены: " + ", ".join(str(value) for value in missing_ids) + "."
            )
        return ids

    def _print_form_errors(self, form: CouponAutomationScenarioCreateForm) -> None:
        self.stdout.write("=== Ошибки создания купонного автосценария ===")
        for field_name, errors in form.errors.items():
            field_label = form.fields.get(field_name).label if field_name in form.fields else field_name
            for error in errors:
                self.stdout.write(f"- {field_label}: {error}")

    def _print_validated_plan(
        self,
        *,
        form: CouponAutomationScenarioCreateForm,
        source_config: CouponAutomationConfig | None,
        confirmed: bool,
    ) -> None:
        cleaned = form.cleaned_data
        template_mode = cleaned.get("template_mode")
        bot_profiles = cleaned.get("notification_bot_profiles") or []
        self.stdout.write("=== Проверка создания купонного автосценария ===")
        self.stdout.write(f"confirmed={confirmed}")
        self.stdout.write(f"scenario_code={cleaned['code']}")
        self.stdout.write(f"scenario_name={cleaned['name']}")
        self.stdout.write(
            "scenario_type="
            f"{dict(CouponAutomationConfig.ScenarioType.choices).get(cleaned['scenario_type'], cleaned['scenario_type'])}"
        )
        if source_config is not None:
            self.stdout.write(f"source_config_id={source_config.id}")
            self.stdout.write(f"source_scenario_code={source_config.scenario.code}")
            self.stdout.write(f"source_rules_count={source_config.coupon_rules.count()}")
        else:
            self.stdout.write("source_config_id=-")
        if template_mode == COUPON_TEMPLATE_MODE_EXISTING:
            template = cleaned.get("existing_template")
            self.stdout.write(f"template=существующий #{template.id}: {template.name}")
        else:
            self.stdout.write(f"template=новый: {cleaned.get('template_name')}")
        self.stdout.write(
            "bot_profiles="
            + (
                ", ".join(f"{bot.id}:{bot.code}" for bot in bot_profiles)
                if bot_profiles
                else "-"
            )
        )
        self.stdout.write("execution_mode=Черновик")
        self.stdout.write("notification_scenario_active=нет")
