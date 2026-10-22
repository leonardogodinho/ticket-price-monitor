#!/usr/bin/env python3
"""Monitor a dynamic ticket page and send an email when its lowest price drops."""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_URL = (
    "https://buyticketbrasil.com/evento/rockinrio2026"
    "?data=1788656400000&evento_local=1765323572984x293448430956314600"
    "&cidade=Rio+de+Janeiro"
)
DEFAULT_SELECTOR = 'span[class*="buyPrice"]'
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Seletor do campo "Tipo de Ingresso" (combobox de autocomplete do BuyTicket).
# A página tem outros comboboxes (ex.: busca de eventos), então miramos pelo placeholder.
DEFAULT_TICKET_TYPE_SELECTOR = 'input[role="combobox"][placeholder="Selecione o tipo de ingresso"]'


class MonitorError(RuntimeError):
    """Raised when the page or notification cannot be processed safely."""


def load_env(path: Path) -> None:
    """Load a small, dependency-free subset of .env syntax."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        return int(raw_value)
    except ValueError as exc:
        raise MonitorError(f"{name} deve ser um número inteiro; recebido: {raw_value!r}") from exc


@dataclass(frozen=True)
class Config:
    url: str
    selector: str
    ticket_type: str
    ticket_type_selector: str
    user_agent: str
    threshold: Decimal
    timeout_ms: int
    state_file: Path
    notification_provider: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str
    smtp_password: str
    email_from: str
    email_to: str
    resend_api_key: str
    resend_from: str

    @classmethod
    def from_env(cls) -> "Config":
        raw_threshold = os.getenv("PRICE_THRESHOLD", "250")
        try:
            threshold = Decimal(raw_threshold.replace(",", "."))
        except InvalidOperation as exc:
            raise MonitorError(
                f"PRICE_THRESHOLD deve ser um valor monetário; recebido: {raw_threshold!r}"
            ) from exc

        state_path = Path(os.getenv("STATE_FILE", str(PROJECT_DIR / ".monitor-state.json")))
        security = os.getenv("SMTP_SECURITY", "starttls").strip().lower()
        if security not in {"starttls", "ssl", "none"}:
            raise MonitorError("SMTP_SECURITY deve ser starttls, ssl ou none")

        provider = os.getenv("NOTIFICATION_PROVIDER", "smtp").strip().lower()
        if provider not in {"smtp", "resend"}:
            raise MonitorError("NOTIFICATION_PROVIDER deve ser smtp ou resend")

        username = os.getenv("SMTP_USERNAME", "").strip()
        return cls(
            url=os.getenv("MONITOR_URL", DEFAULT_URL).strip(),
            selector=os.getenv("PRICE_SELECTOR", DEFAULT_SELECTOR).strip(),
            ticket_type=os.getenv("TICKET_TYPE", "").strip(),
            ticket_type_selector=os.getenv(
                "TICKET_TYPE_SELECTOR", DEFAULT_TICKET_TYPE_SELECTOR
            ).strip(),
            user_agent=os.getenv("USER_AGENT", DEFAULT_USER_AGENT).strip(),
            threshold=threshold,
            timeout_ms=env_int("PAGE_TIMEOUT_SECONDS", 45) * 1000,
            state_file=state_path.expanduser(),
            notification_provider=provider,
            smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com").strip(),
            smtp_port=env_int("SMTP_PORT", 587),
            smtp_security=security,
            smtp_username=username,
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            email_from=os.getenv("EMAIL_FROM", username).strip(),
            email_to=os.getenv("EMAIL_TO", "").strip(),
            resend_api_key=os.getenv("RESEND_API_KEY", "").strip(),
            resend_from=os.getenv(
                "RESEND_FROM", "Monitor de ingressos <onboarding@resend.dev>"
            ).strip(),
        )


def parse_brl(text: str) -> Decimal:
    """Parse common Brazilian price forms such as R$ 249,90 and R$ 1.249,90."""
    value = re.sub(r"[^\d,.-]", "", text.strip())
    if not value or not re.search(r"\d", value):
        raise ValueError(f"texto sem valor numérico: {text!r}")

    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", value):
        value = value.replace(".", "")

    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"formato de preço não reconhecido: {text!r}") from exc


def format_brl(value: Decimal) -> str:
    rendered = f"{value:,.2f}"
    rendered = rendered.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {rendered}"


def fetch_prices(config: Config) -> List[Decimal]:
    """Render the page and return the price(s) to evaluate.

    When ``TICKET_TYPE`` is set, read the price of a single ticket type from the
    "Tipo de Ingresso" combobox. Otherwise, fall back to collecting every price
    matching ``PRICE_SELECTOR``.
    """
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, args=["--disable-blink-features=AutomationControlled"]
            )
            try:
                page = browser.new_page(
                    locale="pt-BR",
                    user_agent=config.user_agent,
                    viewport={"width": 1366, "height": 900},
                )
                wait_until = "networkidle" if config.ticket_type else "domcontentloaded"
                page.goto(config.url, wait_until=wait_until, timeout=config.timeout_ms)
                if config.ticket_type:
                    # The Next.js app hydrates the combobox a moment after load.
                    page.wait_for_timeout(4000)
                    raw_prices = _fetch_ticket_type_prices(page, config)
                else:
                    locator = page.locator(config.selector)
                    locator.first.wait_for(state="attached", timeout=config.timeout_ms)
                    raw_prices = locator.all_inner_texts()
            finally:
                browser.close()
    except MonitorError:
        raise
    except PlaywrightTimeoutError as exc:
        label = (
            f"o tipo de ingresso {config.ticket_type!r}"
            if config.ticket_type
            else f"o seletor {config.selector!r}"
        )
        raise MonitorError(
            f"O preço não apareceu em até {config.timeout_ms // 1000}s usando {label}."
        ) from exc
    except Exception as exc:
        raise MonitorError(f"Falha ao abrir a página monitorada: {exc}") from exc

    prices: List[Decimal] = []
    parse_errors: List[str] = []
    for raw_price in raw_prices:
        try:
            prices.append(parse_brl(raw_price))
        except ValueError:
            parse_errors.append(raw_price)

    if not prices:
        raise MonitorError(
            "Foram encontrados elementos de preço, mas nenhum valor pôde ser lido. "
            f"Conteúdo recebido: {parse_errors!r}"
        )
    return prices


def _normalize(text: str) -> str:
    """Lowercase and collapse whitespace so ticket-type names compare reliably."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _fetch_ticket_type_prices(page: Any, config: Config) -> List[str]:
    """Open the "Tipo de Ingresso" combobox and return the price for the chosen type.

    The BuyTicket combobox is an autocomplete: options for this event only appear
    after typing. Each option renders as ``NOME\\nR$ VALOR``, so we match the option
    whose name equals ``config.ticket_type`` exactly and return just its price text.
    """
    combo = page.locator(config.ticket_type_selector).first
    combo.wait_for(state="visible", timeout=config.timeout_ms)

    wanted = _normalize(config.ticket_type)
    # Type enough of the name to surface the event's ticket types.
    query = config.ticket_type.split()[0] if config.ticket_type.split() else config.ticket_type

    # Shorter per-attempt budget so a slow combobox can't consume the whole timeout.
    option_timeout_ms = min(config.timeout_ms, 15000)

    seen: Dict[str, str] = {}
    last_option_texts: List[str] = []
    for attempt in range(3):
        combo.click()
        combo.fill("")
        page.wait_for_timeout(250)
        combo.type(query, delay=70)
        try:
            page.get_by_role("option").first.wait_for(
                state="visible", timeout=option_timeout_ms
            )
        except PlaywrightTimeoutError:
            if attempt == 2:
                raise
            continue

        page.wait_for_timeout(700)
        options = page.get_by_role("option").all()
        last_option_texts = []
        for option in options:
            try:
                text = option.inner_text().strip()
            except Exception:
                continue
            if not text:
                continue
            last_option_texts.append(text)
            name, _, price = text.partition("\n")
            seen[_normalize(name)] = price.strip() or text

        if wanted in seen:
            break
        page.wait_for_timeout(500)

    if wanted not in seen:
        available = ", ".join(sorted({t.split(chr(10))[0] for t in last_option_texts})) or "nenhuma"
        raise MonitorError(
            f"O tipo de ingresso {config.ticket_type!r} não foi encontrado na lista "
            f"'Tipo de Ingresso'. Opções visíveis: {available}."
        )

    return [seen[wanted]]


def validate_email_config(config: Config) -> None:
    missing = []
    if not config.email_to:
        missing.append("EMAIL_TO")

    if config.notification_provider == "smtp":
        for name, value in (
            ("SMTP_HOST", config.smtp_host),
            ("SMTP_USERNAME", config.smtp_username),
            ("SMTP_PASSWORD", config.smtp_password),
            ("EMAIL_FROM", config.email_from),
        ):
            if not value:
                missing.append(name)
    else:
        for name, value in (
            ("RESEND_API_KEY", config.resend_api_key),
            ("RESEND_FROM", config.resend_from),
        ):
            if not value:
                missing.append(name)

    if missing:
        raise MonitorError(f"Configuração de e-mail incompleta: {', '.join(missing)}")


def send_smtp_email(config: Config, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.email_from
    message["To"] = config.email_to
    message.set_content(body)

    context = ssl.create_default_context()
    try:
        if config.smtp_security == "ssl":
            with smtplib.SMTP_SSL(
                config.smtp_host, config.smtp_port, timeout=30, context=context
            ) as smtp:
                smtp.login(config.smtp_username, config.smtp_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as smtp:
                if config.smtp_security == "starttls":
                    smtp.starttls(context=context)
                smtp.login(config.smtp_username, config.smtp_password)
                smtp.send_message(message)
    except OSError as exc:
        if exc.errno == 65:
            raise MonitorError(
                "A rede bloqueou a conexão SMTP (sem rota para o servidor). "
                "Desative a VPN/rede corporativa ou use NOTIFICATION_PROVIDER=resend."
            ) from exc
        raise MonitorError(f"Falha de rede ao enviar o e-mail por SMTP: {exc}") from exc
    except smtplib.SMTPException as exc:
        raise MonitorError(f"Falha ao enviar o e-mail por SMTP: {exc}") from exc


def send_resend_email(config: Config, subject: str, body: str) -> None:
    payload = json.dumps(
        {
            "from": config.resend_from,
            "to": [config.email_to],
            "subject": subject,
            "text": body,
        }
    ).encode("utf-8")
    request = Request(
        "https://api.resend.com/emails",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {config.resend_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "ticket-price-monitor/1.0",
        },
    )

    try:
        with urlopen(request, timeout=30) as response:
            if not 200 <= response.status < 300:
                raise MonitorError(f"A API Resend retornou HTTP {response.status}")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise MonitorError(f"A API Resend recusou o e-mail (HTTP {exc.code}): {detail}") from exc
    except (URLError, OSError) as exc:
        raise MonitorError(f"Falha de rede ao acessar a API Resend por HTTPS: {exc}") from exc


def send_email(config: Config, subject: str, body: str) -> None:
    validate_email_config(config)
    if config.notification_provider == "resend":
        send_resend_email(config, subject, body)
    else:
        send_smtp_email(config, subject, body)


def read_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
        return content if isinstance(content, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary_path.replace(path)


def check_price(config: Config, dry_run: bool) -> None:
    prices = fetch_prices(config)
    lowest_price = min(prices)
    checked_at = datetime.now(timezone.utc).isoformat()
    below_threshold = lowest_price < config.threshold

    label = f"{config.ticket_type} — " if config.ticket_type else ""
    price_word = "preço" if config.ticket_type else "menor preço"
    print(
        f"[{checked_at}] {label}{len(prices)} preço(s) encontrado(s); "
        f"{price_word}: {format_brl(lowest_price)}; "
        f"limite: {format_brl(config.threshold)}"
    )

    if dry_run:
        print("Modo de teste: nenhum e-mail foi enviado e o estado não foi alterado.")
        return

    previous_state = read_state(config.state_file)
    was_below = bool(previous_state.get("below_threshold", False))
    alert_sent = bool(previous_state.get("alert_sent", False)) if was_below else False

    if below_threshold and not alert_sent:
        type_line = f" ({config.ticket_type})" if config.ticket_type else ""
        subject = (
            f"Ingresso{type_line} abaixo de {format_brl(config.threshold)}: "
            f"{format_brl(lowest_price)}"
        )
        ticket_line = (
            f"Tipo de ingresso: {config.ticket_type}\n" if config.ticket_type else ""
        )
        body = (
            "O monitor encontrou um ingresso dentro do preço desejado.\n\n"
            f"{ticket_line}"
            f"Menor preço atual: {format_brl(lowest_price)}\n"
            f"Limite configurado: {format_brl(config.threshold)}\n"
            f"Preços encontrados: {', '.join(format_brl(price) for price in prices)}\n"
            f"Página: {config.url}\n"
            f"Verificado em: {checked_at}\n\n"
            "O preço pode mudar rapidamente; confirme o valor no site antes da compra."
        )
        send_email(config, subject, body)
        alert_sent = True
        print(f"Alerta enviado para {config.email_to}.")
    elif below_threshold:
        print("O preço continua abaixo do limite; alerta já enviado para esta queda.")
    elif was_below:
        alert_sent = False
        print("O preço voltou ao limite ou acima; um novo alerta poderá ser enviado na próxima queda.")

    write_state(
        config.state_file,
        {
            "last_checked_at": checked_at,
            "last_price": str(lowest_price),
            "below_threshold": below_threshold,
            "alert_sent": alert_sent,
        },
    )


def test_email(config: Config) -> None:
    sent_at = datetime.now(timezone.utc).isoformat()
    send_email(
        config,
        "Teste do monitor de preço do ingresso",
        f"A configuração de e-mail está funcionando.\nEnviado em: {sent_at}\n",
    )
    print(f"E-mail de teste enviado para {config.email_to}.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--dry-run",
        action="store_true",
        help="consulta e exibe o preço sem enviar e-mail ou alterar o estado",
    )
    action.add_argument(
        "--test-email",
        action="store_true",
        help="envia somente um e-mail de teste, sem consultar a página",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env(PROJECT_DIR / ".env")
    try:
        config = Config.from_env()
        if args.test_email:
            test_email(config)
        else:
            check_price(config, dry_run=args.dry_run)
        return 0
    except MonitorError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
