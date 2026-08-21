import os
import re
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

from flask import Flask, render_template, request, redirect, session, send_file, url_for

from dotenv import load_dotenv

from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from database import (
    create_database,
    add_user,
    check_user,
    add_document,
    get_documents,
    get_document,
    update_document,
    delete_document,
    get_total_documents_count,
    create_reset_token,
    get_user_by_reset_token,
    update_password,
    get_user_by_username,
    update_username,
    change_password,
    username_taken,
    get_usage_info,
    can_generate,
    increment_generations_used,
    get_referral_info,
    referral_code_exists
)

from io import BytesIO

from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.pagesizes import A4

import ollama


load_dotenv()

app = Flask(__name__)

# Секретный ключ ТОЛЬКО из .env — без него приложение не должно стартовать
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")

if not SECRET_KEY:
    raise RuntimeError(
        "FLASK_SECRET_KEY не задан в .env! "
        "Сгенерируй его: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

app.secret_key = SECRET_KEY

DEBUG_MODE = os.environ.get("FLASK_DEBUG", "False").lower() == "true"

# Безопасность сессионных cookie: недоступны из JS, не улетают на сторонние сайты,
# и передаются только по HTTPS, если сайт уже задеплоен не в debug-режиме
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not DEBUG_MODE,
)


DATABASE = "users.db"

DOCUMENT_TYPES = [
    "Резюме",
    "Договор",
    "Заявление",
    "Коммерческое предложение",
]

MAX_DESCRIPTION_LENGTH = 2000


create_database()


pdfmetrics.registerFont(TTFont("DejaVuSans", "fonts/DejaVuSans.ttf"))


csrf = CSRFProtect(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[]
)


@app.after_request
def set_security_headers(response):
    """Базовые заголовки безопасности для каждого ответа."""

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    return response


@app.errorhandler(404)
def not_found(e):
    return render_template(
        "error.html",
        code=404,
        heading="Страница не найдена",
        message="Такой страницы не существует или она была перемещена.",
        user=session.get("user")
    ), 404


@app.errorhandler(500)
def server_error(e):
    return render_template(
        "error.html",
        code=500,
        heading="Что-то пошло не так",
        message="На сервере произошла ошибка. Попробуйте ещё раз чуть позже.",
        user=session.get("user")
    ), 500


@app.errorhandler(429)
def rate_limited(e):
    return render_template(
        "error.html",
        code=429,
        heading="Слишком много попыток",
        message="Немного подождите и попробуйте снова.",
        user=session.get("user")
    ), 429


def send_reset_email(email, reset_link):
    """
    Отправляет письмо со ссылкой для сброса пароля через Gmail SMTP (бесплатно).
    Нужно задать в .env: SMTP_EMAIL и SMTP_PASSWORD (ПАРОЛЬ ПРИЛОЖЕНИЯ Gmail, не обычный пароль).
    Если не настроено — ссылка печатается в консоль сервера (только для разработки!).
    """

    smtp_email = os.environ.get("SMTP_EMAIL")
    smtp_password = os.environ.get("SMTP_PASSWORD")

    if not smtp_email or not smtp_password:
        print("=" * 60)
        print("SMTP не настроен (нет SMTP_EMAIL/SMTP_PASSWORD в .env).")
        print(f"Ссылка для сброса пароля ({email}): {reset_link}")
        print("=" * 60)
        return

    message = MIMEText(
        f"Вы запросили сброс пароля на DocPilot AI.\n\n"
        f"Ссылка действительна 1 час:\n{reset_link}\n\n"
        f"Если это были не вы — просто проигнорируйте это письмо.",
        "plain",
        "utf-8"
    )

    message["Subject"] = "Сброс пароля — DocPilot AI"
    message["From"] = smtp_email
    message["To"] = email

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(smtp_email, smtp_password)
        server.sendmail(smtp_email, email, message.as_string())


def generate_document(document_type, description):

    response = ollama.chat(
        model="llama3.2:3b",
        messages=[
            {
                "role": "user",
                "content": f"""Ты — профессиональный составитель документов.
Составь готовый документ типа "{document_type}".

Требования пользователя:
{description}

СТРОГО СОБЛЮДАЙ ЭТИ ПРАВИЛА:
1. Весь текст — ТОЛЬКО на грамотном русском языке. Никаких английских слов или смешения языков внутри текста.
2. Если какие-то данные неизвестны (ФИО, адрес, дата, название организации) — обозначай их короткими русскими плейсхолдерами в квадратных скобках, например: [ФИО], [Дата], [Адрес], [Название организации]. Не используй английские слова внутри плейсхолдеров.
3. Структура должна быть логичной и соответствовать типу документа.
4. Выведи ТОЛЬКО готовый текст документа, без пояснений от себя и без markdown-разметки."""
            }
        ]
    )

    return response["message"]["content"]


def format_date(iso_string):
    """Превращает ISO-дату в человеко-читаемый вид. Терпимо относится к отсутствию даты (старые записи)."""

    if not iso_string:
        return "—"

    try:
        dt = datetime.fromisoformat(iso_string)
        return dt.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return "—"


app.jinja_env.filters["format_date"] = format_date


EMAIL_REGEX = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@app.route("/telderieec389dd698372a4a68a66a4904e5e15.txt")
def telderi_verify():
    return send_file(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "telderieec389dd698372a4a68a66a4904e5e15.txt"),
        mimetype="text/plain"
    )
    return render_template(
        "index.html",
        user=session.get("user"),
        total_count=get_total_documents_count(),
        og_title="DocPilot AI — документы за секунды",
        og_description="AI генерирует резюме, договоры, заявления и коммерческие предложения за секунды. Готовый документ — сразу в PDF."
    )


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def register():

    ref_code = request.args.get("ref", "").strip().upper() or request.form.get("ref_code", "").strip().upper()

    if request.method == "POST":

        username = request.form["username"].strip()

        email = request.form["email"].strip().lower()

        password = request.form["password"]

        password_confirm = request.form.get("password_confirm", "")

        # Валидация — проверяем до того, как пытаться создать юзера
        if len(username) < 2:
            return render_template(
                "register.html",
                error="Имя должно содержать минимум 2 символа",
                ref_code=ref_code
            )

        if not EMAIL_REGEX.match(email):
            return render_template(
                "register.html",
                error="Введите корректный email",
                ref_code=ref_code
            )

        if len(password) < 6:
            return render_template(
                "register.html",
                error="Пароль должен быть не менее 6 символов",
                ref_code=ref_code
            )

        if password != password_confirm:
            return render_template(
                "register.html",
                error="Пароли не совпадают",
                ref_code=ref_code
            )

        success = add_user(
            username,
            email,
            password,
            referred_by_code=ref_code if ref_code and referral_code_exists(ref_code) else None
        )

        if not success:

            if username_taken(username):
                error = "Это имя уже занято — выберите другое"
            else:
                error = "Этот email уже зарегистрирован"

            return render_template(
                "register.html",
                error=error,
                ref_code=ref_code,
                user=session.get("user")
            )

        return redirect("/login")

    return render_template("register.html", ref_code=ref_code, user=session.get("user"))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def login():

    if request.method == "POST":

        email = request.form["email"].strip().lower()

        password = request.form["password"]

        user = check_user(
            email,
            password
        )

        if not user:

            return render_template(
                "login.html",
                error="Неверный email или пароль",
                user=session.get("user")
            )

        session["user"] = user[1]

        return redirect("/dashboard")

    return render_template("login.html", user=session.get("user"))


@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("3 per hour")
def forgot_password():

    if request.method == "POST":

        email = request.form["email"].strip().lower()

        token = create_reset_token(email)

        if token:

            reset_link = url_for("reset_password", token=token, _external=True)

            try:
                send_reset_email(email, reset_link)
            except Exception as e:
                print(f"Ошибка отправки письма: {e}")

        # Одинаковое сообщение вне зависимости от того, найден ли email — чтобы нельзя было проверить, зарегистрирован ли он
        return render_template(
            "forgot_password.html",
            message="Если такой email зарегистрирован, мы отправили на него ссылку для сброса пароля.",
            user=session.get("user")
        )

    return render_template("forgot_password.html", user=session.get("user"))


@app.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("20 per hour")
def reset_password(token):

    user = get_user_by_reset_token(token)

    if not user:
        return render_template(
            "forgot_password.html",
            message="Ссылка недействительна или истекла. Запросите новую.",
            user=session.get("user")
        )

    if request.method == "POST":

        new_password = request.form["password"]

        password_confirm = request.form.get("password_confirm", "")

        if len(new_password) < 6:
            return render_template(
                "reset_password.html",
                error="Пароль должен быть не менее 6 символов",
                user=session.get("user")
            )

        if new_password != password_confirm:
            return render_template(
                "reset_password.html",
                error="Пароли не совпадают",
                user=session.get("user")
            )

        update_password(user[2], new_password)

        return redirect("/login")

    return render_template("reset_password.html", user=session.get("user"))


@app.route("/dashboard")
def dashboard():

    if "user" not in session:

        return redirect("/login")

    return render_template(
        "dashboard.html",
        user=session["user"],
        usage=get_usage_info(session["user"])
    )


@app.route("/settings")
def settings():

    if "user" not in session:

        return redirect("/login")

    account = get_user_by_username(session["user"])

    if not account:
        session.clear()
        return redirect("/login")

    return render_template(
        "settings.html",
        user=session["user"],
        email=account[2],
        usage=get_usage_info(session["user"]),
        referral=get_referral_info(session["user"])
    )


@app.route("/settings/profile", methods=["POST"])
@limiter.limit("10 per hour")
def settings_profile():

    if "user" not in session:

        return redirect("/login")

    new_username = request.form["username"].strip()

    account = get_user_by_username(session["user"])

    email = account[2] if account else ""

    if len(new_username) < 2:
        return render_template(
            "settings.html",
            user=session["user"],
            email=email,
            usage=get_usage_info(session["user"]),
            referral=get_referral_info(session["user"]),
            error="Имя должно содержать минимум 2 символа"
        )

    updated = update_username(session["user"], new_username)

    if not updated:
        return render_template(
            "settings.html",
            user=session["user"],
            email=email,
            usage=get_usage_info(session["user"]),
            referral=get_referral_info(session["user"]),
            error="Это имя уже занято — выберите другое"
        )

    session["user"] = new_username

    return render_template(
        "settings.html",
        user=session["user"],
        email=email,
        usage=get_usage_info(session["user"]),
        referral=get_referral_info(session["user"]),
        success="Имя обновлено"
    )


@app.route("/settings/password", methods=["POST"])
@limiter.limit("10 per hour")
def settings_password():

    if "user" not in session:

        return redirect("/login")

    account = get_user_by_username(session["user"])

    email = account[2] if account else ""

    current_password = request.form["current_password"]

    new_password = request.form["new_password"]

    new_password_confirm = request.form.get("new_password_confirm", "")

    if len(new_password) < 6:
        return render_template(
            "settings.html",
            user=session["user"],
            email=email,
            usage=get_usage_info(session["user"]),
            referral=get_referral_info(session["user"]),
            error="Новый пароль должен быть не менее 6 символов"
        )

    if new_password != new_password_confirm:
        return render_template(
            "settings.html",
            user=session["user"],
            email=email,
            usage=get_usage_info(session["user"]),
            referral=get_referral_info(session["user"]),
            error="Пароли не совпадают"
        )

    success = change_password(session["user"], current_password, new_password)

    if not success:
        return render_template(
            "settings.html",
            user=session["user"],
            email=email,
            usage=get_usage_info(session["user"]),
            referral=get_referral_info(session["user"]),
            error="Текущий пароль указан неверно"
        )

    return render_template(
        "settings.html",
        user=session["user"],
        email=email,
        usage=get_usage_info(session["user"]),
        referral=get_referral_info(session["user"]),
        success="Пароль обновлён"
    )


@app.route("/pro")
def pro():

    return render_template(
        "pro.html",
        user=session.get("user"),
        usage=get_usage_info(session["user"]) if "user" in session else None,
        referral=get_referral_info(session["user"]) if "user" in session else None
    )


@app.route("/create", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def create():

    if "user" not in session:

        return redirect("/login")

    usage = get_usage_info(session["user"])

    if request.method == "POST":

        if not can_generate(session["user"]):
            return render_template(
                "create.html",
                user=session["user"],
                usage=usage,
                error="Бесплатные генерации закончились. Оформите Pro или пригласите друга по реферальной ссылке, чтобы получить ещё генерации."
            )

        document_type = request.form.get("type", "")

        description = request.form.get("description", "").strip()

        if document_type not in DOCUMENT_TYPES:
            return render_template(
                "create.html",
                user=session["user"],
                usage=usage,
                error="Выберите тип документа из списка"
            )

        if not description:
            return render_template(
                "create.html",
                user=session["user"],
                usage=usage,
                error="Опишите, какой документ нужно создать"
            )

        if len(description) > MAX_DESCRIPTION_LENGTH:
            return render_template(
                "create.html",
                user=session["user"],
                usage=usage,
                error=f"Описание слишком длинное (максимум {MAX_DESCRIPTION_LENGTH} символов)"
            )

        try:
            content = generate_document(document_type, description)
        except Exception:
            return render_template(
                "create.html",
                user=session["user"],
                usage=usage,
                error="Не удалось связаться с AI-моделью. Убедитесь, что Ollama запущена, и попробуйте снова."
            )

        add_document(
            session["user"],
            document_type,
            content
        )

        increment_generations_used(session["user"])

        return redirect("/documents")

    return render_template("create.html", user=session["user"], usage=usage)


@app.route("/documents")
def documents():

    if "user" not in session:

        return redirect("/login")

    docs = get_documents(
        session["user"]
    )

    return render_template(
        "documents.html",
        user=session["user"],
        documents=docs
    )


@app.route("/document/<int:id>")
def open_document(id):

    if "user" not in session:

        return redirect("/login")

    document = get_document(id, session["user"])

    if not document:
        return render_template(
            "error.html",
            code=404,
            heading="Документ не найден",
            message="Такого документа нет в вашем аккаунте, либо он был удалён.",
            user=session.get("user")
        ), 404

    return render_template(
        "result.html",
        user=session["user"],
        title=document[1],
        content=document[2],
        document_id=document[0],
        doc_number=document[4],
        created_at=format_date(document[3])
    )


@app.route("/document/<int:id>/edit", methods=["POST"])
@limiter.limit("30 per hour")
def edit_document(id):

    if "user" not in session:

        return redirect("/login")

    new_content = request.form.get("content", "").strip()

    if new_content:
        update_document(id, session["user"], new_content)

    return redirect(f"/document/{id}")


@app.route("/document/<int:id>/delete", methods=["POST"])
@limiter.limit("30 per hour")
def delete_document_route(id):

    if "user" not in session:

        return redirect("/login")

    delete_document(id, session["user"])

    return redirect("/documents")


def draw_wrapped_text(text_object, text, canvas_obj, font_name, font_size, max_width):
    """Печатает текст в PDF с переносом строк, чтобы длинные абзацы не вылезали за край страницы."""

    for paragraph in text.split("\n"):

        if not paragraph.strip():
            text_object.textLine("")
            continue

        words = paragraph.split(" ")
        line = ""

        for word in words:

            candidate = f"{line} {word}".strip()

            if canvas_obj.stringWidth(candidate, font_name, font_size) <= max_width:
                line = candidate
            else:
                text_object.textLine(line)
                line = word

        if line:
            text_object.textLine(line)


@app.route("/download/<int:id>")
def download_pdf(id):

    if "user" not in session:

        return redirect("/login")

    document = get_document(id, session["user"])

    if not document:
        return render_template(
            "error.html",
            code=404,
            heading="Документ не найден",
            message="Такого документа нет в вашем аккаунте, либо он был удалён.",
            user=session.get("user")
        ), 404

    title = document[1]

    content = document[2]

    pdf = BytesIO()

    file = canvas.Canvas(pdf, pagesize=A4)

    page_width, page_height = A4

    file.setFont("DejaVuSans", 16)

    file.drawString(50, page_height - 60, title)

    text = file.beginText(50, page_height - 100)

    text.setFont("DejaVuSans", 11)

    draw_wrapped_text(text, content, file, "DejaVuSans", 11, page_width - 100)

    file.drawText(text)

    file.save()

    pdf.seek(0)

    safe_name = re.sub(r"[^a-zA-Zа-яА-Я0-9_-]+", "_", title).strip("_") or "document"

    return send_file(
        pdf,
        as_attachment=True,
        download_name=f"{safe_name}_{id}.pdf",
        mimetype="application/pdf"
    )


@app.route("/logout")
def logout():

    session.clear()

    return redirect("/")


if __name__ == "__main__":

    app.run(debug=DEBUG_MODE)
