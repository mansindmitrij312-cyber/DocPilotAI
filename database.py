import sqlite3
import secrets
import string
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

DATABASE = "users.db"

FREE_GENERATIONS_LIMIT = 5

REFERRAL_BONUS_GENERATIONS = 5


def create_database():

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        password TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL
    )
    """)

    # Миграции — добавляем колонки, если их ещё нет (CREATE TABLE IF NOT EXISTS не тронет уже существующую таблицу)
    for table, column, col_type in [
        ("users", "reset_token", "TEXT"),
        ("users", "reset_token_expiry", "TEXT"),
        ("users", "plan", "TEXT DEFAULT 'free'"),
        ("users", "generations_used", "INTEGER DEFAULT 0"),
        ("users", "bonus_generations", "INTEGER DEFAULT 0"),
        ("users", "referral_code", "TEXT"),
        ("users", "referred_by", "TEXT"),
        ("documents", "created_at", "TEXT"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except sqlite3.OperationalError:
            pass  # колонка уже существует

    connection.commit()

    # Донабиваем реферальные коды тем, у кого их ещё нет (старые записи / только что добавленная колонка)
    cursor.execute("SELECT username FROM users WHERE referral_code IS NULL OR referral_code=''")
    missing = cursor.fetchall()

    for (username,) in missing:
        code = _generate_unique_referral_code(cursor)
        cursor.execute("UPDATE users SET referral_code=? WHERE username=?", (code, username))

    connection.commit()
    connection.close()


def _generate_unique_referral_code(cursor):
    """Генерирует уникальный короткий реферальный код. Вызывается с открытым курсором внутри той же транзакции."""

    alphabet = string.ascii_uppercase + string.digits

    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))

        cursor.execute("SELECT id FROM users WHERE referral_code=?", (code,))

        if not cursor.fetchone():
            return code


def username_taken(username, exclude_email=None):
    """Проверяет, занято ли имя пользователя (без учёта регистра). exclude_email — не учитывать этого юзера (для смены имени самим собой)."""

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    if exclude_email:
        cursor.execute(
            "SELECT id FROM users WHERE LOWER(username)=LOWER(?) AND email<>?",
            (username, exclude_email)
        )
    else:
        cursor.execute(
            "SELECT id FROM users WHERE LOWER(username)=LOWER(?)",
            (username,)
        )

    taken = cursor.fetchone() is not None

    connection.close()

    return taken


def add_user(username, email, password, referred_by_code=None):
    """Создаёт юзера. Если передан валидный referral_code другого юзера — привязывает
    рефералку и начисляет пригласившему бонусные генерации."""

    if username_taken(username):
        return False

    password_hash = generate_password_hash(password)

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    try:

        own_code = _generate_unique_referral_code(cursor)

        referred_by_username = None

        if referred_by_code:
            cursor.execute(
                "SELECT username FROM users WHERE referral_code=?",
                (referred_by_code,)
            )
            row = cursor.fetchone()
            if row:
                referred_by_username = row[0]

        cursor.execute(
            """
            INSERT INTO users (username, email, password, referral_code, referred_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                username,
                email,
                password_hash,
                own_code,
                referred_by_username
            )
        )

        if referred_by_username:
            cursor.execute(
                "UPDATE users SET bonus_generations = bonus_generations + ? WHERE username=?",
                (REFERRAL_BONUS_GENERATIONS, referred_by_username)
            )

        connection.commit()

        return True

    except sqlite3.IntegrityError:

        return False

    finally:

        connection.close()


def check_user(email, password):

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT *
        FROM users
        WHERE email=?
        """,
        (email,)
    )

    user = cursor.fetchone()

    connection.close()

    if user and check_password_hash(user[3], password):
        return user

    return None


def get_user_by_username(username):

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT id, username, email, password
        FROM users
        WHERE username=?
        """,
        (username,)
    )

    user = cursor.fetchone()

    connection.close()

    return user


def update_username(old_username, new_username):
    """Переименовывает юзера и каскадно обновляет владельца во всех его документах.
    Возвращает False, если новое имя уже занято кем-то другим."""

    if old_username == new_username:
        return True

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        "SELECT id FROM users WHERE LOWER(username)=LOWER(?) AND username<>?",
        (new_username, old_username)
    )

    if cursor.fetchone():
        connection.close()
        return False

    cursor.execute(
        "UPDATE users SET username=? WHERE username=?",
        (new_username, old_username)
    )

    cursor.execute(
        "UPDATE documents SET username=? WHERE username=?",
        (new_username, old_username)
    )

    cursor.execute(
        "UPDATE users SET referred_by=? WHERE referred_by=?",
        (new_username, old_username)
    )

    connection.commit()
    connection.close()

    return True


def change_password(username, current_password, new_password):
    """Меняет пароль, предварительно сверяя текущий. Возвращает True/False."""

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        "SELECT password FROM users WHERE username=?",
        (username,)
    )

    row = cursor.fetchone()

    if not row or not check_password_hash(row[0], current_password):
        connection.close()
        return False

    new_hash = generate_password_hash(new_password)

    cursor.execute(
        "UPDATE users SET password=? WHERE username=?",
        (new_hash, username)
    )

    connection.commit()
    connection.close()

    return True


def create_reset_token(email):
    """Генерирует одноразовый токен сброса пароля. Возвращает токен, если email найден, иначе None."""

    token = secrets.token_urlsafe(32)
    expiry = (datetime.utcnow() + timedelta(hours=1)).isoformat()

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE users
        SET reset_token=?, reset_token_expiry=?
        WHERE email=?
        """,
        (token, expiry, email)
    )

    connection.commit()

    updated = cursor.rowcount > 0

    connection.close()

    return token if updated else None


def get_user_by_reset_token(token):
    """Возвращает юзера, если токен валиден и не истёк. Иначе None."""

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT id, username, email, password, reset_token, reset_token_expiry
        FROM users
        WHERE reset_token=?
        """,
        (token,)
    )

    user = cursor.fetchone()

    connection.close()

    if not user:
        return None

    expiry = user[5]

    if not expiry or datetime.fromisoformat(expiry) < datetime.utcnow():
        return None

    return user


def update_password(email, new_password):
    """Обновляет пароль и сбрасывает использованный токен."""

    password_hash = generate_password_hash(new_password)

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE users
        SET password=?, reset_token=NULL, reset_token_expiry=NULL
        WHERE email=?
        """,
        (password_hash, email)
    )

    connection.commit()
    connection.close()


def add_document(username, title, content):

    created_at = datetime.utcnow().isoformat()

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT INTO documents(username,title,content,created_at)
        VALUES(?,?,?,?)
        """,
        (
            username,
            title,
            content,
            created_at
        )
    )

    connection.commit()
    connection.close()


def get_documents(username):

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT id, title, content, created_at,
        (SELECT COUNT(*) FROM documents d2 WHERE d2.username=documents.username AND d2.id<=documents.id) AS seq
        FROM documents
        WHERE username=?
        ORDER BY id DESC
        """,
        (username,)
    )

    documents = cursor.fetchall()

    connection.close()

    return documents


def get_document(document_id, username):

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT id, title, content, created_at,
        (SELECT COUNT(*) FROM documents d2 WHERE d2.username=documents.username AND d2.id<=documents.id) AS seq
        FROM documents
        WHERE id=? AND username=?
        """,
        (document_id, username)
    )

    document = cursor.fetchone()

    connection.close()

    return document


def update_document(document_id, username, content):
    """Обновляет текст документа. Возвращает True, если документ найден и принадлежит юзеру."""

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE documents
        SET content=?
        WHERE id=? AND username=?
        """,
        (content, document_id, username)
    )

    connection.commit()

    updated = cursor.rowcount > 0

    connection.close()

    return updated


def delete_document(document_id, username):
    """Удаляет документ. Возвращает True, если документ был найден и принадлежал юзеру."""

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        "DELETE FROM documents WHERE id=? AND username=?",
        (document_id, username)
    )

    connection.commit()

    deleted = cursor.rowcount > 0

    connection.close()

    return deleted


def get_total_documents_count():
    """Общее количество документов на сайте (для счётчика на главной странице)."""

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute("SELECT COUNT(*) FROM documents")

    count = cursor.fetchone()[0]

    connection.close()

    return count


def get_usage_info(username):
    """Возвращает словарь с тарифом и остатком генераций для юзера."""

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        "SELECT plan, generations_used, bonus_generations FROM users WHERE username=?",
        (username,)
    )

    row = cursor.fetchone()

    connection.close()

    if not row:
        return {
            "plan": "free",
            "used": 0,
            "limit": FREE_GENERATIONS_LIMIT,
            "remaining": FREE_GENERATIONS_LIMIT,
            "is_pro": False
        }

    plan, used, bonus = row

    used = used or 0

    bonus = bonus or 0

    is_pro = plan == "pro"

    effective_limit = FREE_GENERATIONS_LIMIT + bonus

    remaining = None if is_pro else max(0, effective_limit - used)

    return {
        "plan": plan or "free",
        "used": used,
        "limit": effective_limit,
        "remaining": remaining,
        "is_pro": is_pro
    }


def can_generate(username):
    """True, если у юзера ещё остались генерации (Pro — всегда True)."""

    info = get_usage_info(username)

    return info["is_pro"] or info["remaining"] > 0


def increment_generations_used(username):

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        "UPDATE users SET generations_used = generations_used + 1 WHERE username=?",
        (username,)
    )

    connection.commit()
    connection.close()


def get_referral_info(username):
    """Возвращает реферальный код юзера и количество приглашённых им людей."""

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        "SELECT referral_code, bonus_generations FROM users WHERE username=?",
        (username,)
    )

    row = cursor.fetchone()

    if not row:
        connection.close()
        return {"code": None, "invited_count": 0, "bonus_generations": 0}

    code, bonus = row

    cursor.execute(
        "SELECT COUNT(*) FROM users WHERE referred_by=?",
        (username,)
    )

    invited_count = cursor.fetchone()[0]

    connection.close()

    return {
        "code": code,
        "invited_count": invited_count,
        "bonus_generations": bonus or 0
    }


def referral_code_exists(code):

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute("SELECT id FROM users WHERE referral_code=?", (code,))

    exists = cursor.fetchone() is not None

    connection.close()

    return exists
