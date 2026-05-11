import base64
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pymysql
import streamlit as st

from database import (
    init_db, create_user, verify_login, get_user,
    update_user_profile, update_password,
    update_avatar, delete_user,
    create_reset_token, validate_reset_token, use_reset_token,
)

st.set_page_config(page_title="GroupQuest", page_icon="🏆", layout="centered")

init_db()

# --- Session state defaults ---
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "username" not in st.session_state:
    st.session_state.username = None
if "user_id" not in st.session_state:
    st.session_state.user_id = None
if "show_profile" not in st.session_state:
    st.session_state.show_profile = False


# --- E-Mail helper ---
def _send_reset_email(to_address: str, username: str, reset_link: str) -> None:
    """Send password-reset e-mail via SMTP (configured in secrets.toml [email])."""
    cfg = st.secrets["email"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "GroupQuest – Passwort zurücksetzen"
    msg["From"] = cfg["from_address"]
    msg["To"] = to_address

    text = (
        f"Hallo {username},\n\n"
        f"du hast eine Passwortzurücksetzung für dein GroupQuest-Konto angefordert.\n\n"
        f"Klicke auf folgenden Link, um ein neues Passwort zu setzen (gültig für 1 Stunde):\n"
        f"{reset_link}\n\n"
        f"Falls du diese Anfrage nicht gestellt hast, kannst du diese E-Mail ignorieren.\n\n"
        f"Dein GroupQuest-Team"
    )
    html = (
        f"<p>Hallo <strong>{username}</strong>,</p>"
        f"<p>du hast eine Passwortzurücksetzung für dein GroupQuest-Konto angefordert.</p>"
        f"<p><a href='{reset_link}'>Passwort jetzt zurücksetzen</a> (gültig für 1 Stunde)</p>"
        f"<p>Falls du diese Anfrage nicht gestellt hast, kannst du diese E-Mail ignorieren.</p>"
        f"<p>Dein GroupQuest-Team</p>"
    )
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"])) as server:
        server.ehlo()
        server.starttls()
        server.login(cfg["smtp_user"], cfg["smtp_password"])
        server.sendmail(cfg["from_address"], to_address, msg.as_string())


# --- Password reset page (token from URL) ---
def show_reset_password_page(token: str):
    st.title("GroupQuest 🏆")
    st.subheader("Neues Passwort setzen")

    user_id = validate_reset_token(token)
    if not user_id:
        st.error("Dieser Link ist ungültig oder abgelaufen. Bitte fordere einen neuen Reset-Link an.")
        if st.button("Zurück zum Login"):
            st.query_params.clear()
            st.rerun()
        return

    with st.form("set_new_password_form"):
        new_pw = st.text_input("Neues Passwort", type="password")
        new_pw2 = st.text_input("Neues Passwort bestätigen", type="password")
        submitted = st.form_submit_button("Passwort speichern", use_container_width=True)

    if submitted:
        if not new_pw or not new_pw2:
            st.error("Bitte beide Felder ausfüllen.")
        elif new_pw != new_pw2:
            st.error("Passwörter stimmen nicht überein.")
        elif len(new_pw) < 8:
            st.error("Passwort muss mindestens 8 Zeichen lang sein.")
        else:
            ok = use_reset_token(token, new_pw)
            if ok:
                user = get_user(user_id)
                st.session_state.logged_in = True
                st.session_state.username = user["username"]
                st.session_state.user_id = user["id"]
                st.query_params.clear()
                st.rerun()
            else:
                st.error("Der Link ist leider abgelaufen. Bitte fordere einen neuen an.")


# --- Auth page ---
def show_auth_page():
    st.title("GroupQuest 🏆")

    tab_login, tab_register = st.tabs(["Einloggen", "Account erstellen"])

    with tab_login:
        with st.form("login_form"):
            username = st.text_input("Benutzername oder E-Mail")
            password = st.text_input("Passwort", type="password")
            submitted = st.form_submit_button("Einloggen", use_container_width=True)

        if submitted:
            if not username or not password:
                st.error("Bitte alle Felder ausfüllen.")
            else:
                user = verify_login(username, password)
                if user:
                    st.session_state.logged_in = True
                    st.session_state.username = user["username"]
                    st.session_state.user_id = user["id"]
                    st.rerun()
                else:
                    st.error("Ungültiger Benutzername oder falsches Passwort.")

        with st.expander("Passwort vergessen?"):
            st.write("Gib deine E-Mail-Adresse ein. Du erhältst einen Link zum Zurücksetzen.")
            with st.form("forgot_password_form"):
                reset_email = st.text_input("E-Mail-Adresse")
                submitted_reset = st.form_submit_button("Reset-Link senden", use_container_width=True)

            if submitted_reset:
                if not reset_email:
                    st.error("Bitte E-Mail-Adresse eingeben.")
                else:
                    result = create_reset_token(reset_email)
                    # Always show success to avoid user enumeration
                    if result:
                        token, uname = result
                        app_url = st.secrets.get("email", {}).get("app_url", "")
                        reset_link = f"{app_url}?reset_token={token}"
                        is_local = "localhost" in app_url or "127.0.0.1" in app_url
                        if is_local:
                            st.info(f"**Lokaler Testmodus** – kein E-Mail-Versand.\n\n**Reset-Link:**\n{reset_link}")
                            st.stop()
                        try:
                            _send_reset_email(reset_email, uname, reset_link)
                        except Exception as e:
                            st.error(f"E-Mail konnte nicht gesendet werden: {e}")
                            st.stop()
                    st.success("Falls ein Konto mit dieser E-Mail existiert, wurde ein Reset-Link gesendet.")

    with tab_register:
        with st.form("register_form"):
            new_username = st.text_input("Benutzername")
            new_email = st.text_input("E-Mail")
            new_password = st.text_input("Passwort", type="password")
            confirm_password = st.text_input("Passwort bestätigen", type="password")
            submitted = st.form_submit_button("Account erstellen", use_container_width=True)

        if submitted:
            if not all([new_username, new_email, new_password, confirm_password]):
                st.error("Bitte alle Felder ausfüllen.")
            elif new_password != confirm_password:
                st.error("Passwörter stimmen nicht überein.")
            elif len(new_password) < 8:
                st.error("Passwort muss mindestens 8 Zeichen lang sein.")
            else:
                try:
                    create_user(new_username, new_email, new_password)
                    user = verify_login(new_username, new_password)
                    st.session_state.logged_in = True
                    st.session_state.username = user["username"]
                    st.session_state.user_id = user["id"]
                    st.rerun()
                except pymysql.IntegrityError:
                    st.error("Benutzername oder E-Mail ist bereits vergeben.")


# --- Profile page ---
def show_profile_page():
    st.title("Mein Profil")
    if st.button("Zurück zum Dashboard"):
        st.session_state.show_profile = False
        st.rerun()

    user = get_user(st.session_state.user_id)
    if not user:
        st.error("Profil konnte nicht geladen werden.")
        return

    col_avatar, col_info = st.columns([1, 3])
    with col_avatar:
        if user.get("avatar"):
            img_bytes = base64.b64decode(user["avatar"])
            st.image(img_bytes, width=100)
        else:
            st.markdown("### 👤")
    with col_info:
        st.markdown(f"**{user.get('display_name') or user['username']}**")
        st.caption(f"@{user['username']} · {user['email']}")
        if user.get("bio"):
            st.write(user["bio"])

    st.divider()

    tab_profile, tab_avatar, tab_password, tab_delete = st.tabs([
        "Profil bearbeiten", "Profilbild", "Passwort ändern", "Account löschen"
    ])

    # --- Tab: Profil bearbeiten ---
    with tab_profile:
        with st.form("edit_profile_form"):
            new_username = st.text_input("Benutzername", value=user["username"])
            new_email = st.text_input("E-Mail", value=user["email"])
            new_display = st.text_input("Anzeigename", value=user.get("display_name") or "")
            new_bio = st.text_area("Bio (max. 500 Zeichen)", value=user.get("bio") or "", max_chars=500)
            submitted = st.form_submit_button("Speichern", use_container_width=True)

        if submitted:
            if not new_username or not new_email:
                st.error("Benutzername und E-Mail dürfen nicht leer sein.")
            else:
                try:
                    update_user_profile(st.session_state.user_id, new_username, new_email, new_display, new_bio)
                    st.session_state.username = new_username
                    st.success("Profil erfolgreich gespeichert.")
                    st.rerun()
                except pymysql.IntegrityError:
                    st.error("Benutzername oder E-Mail ist bereits vergeben.")

    # --- Tab: Profilbild ---
    with tab_avatar:
        st.write("Lade ein Bild hoch (JPG oder PNG, max. 2 MB).")
        uploaded = st.file_uploader("Profilbild auswählen", type=["jpg", "jpeg", "png"])
        if uploaded:
            if uploaded.size > 2 * 1024 * 1024:
                st.error("Das Bild darf maximal 2 MB groß sein.")
            else:
                st.image(uploaded, width=150, caption="Vorschau")
                if st.button("Profilbild speichern"):
                    update_avatar(st.session_state.user_id, uploaded.read())
                    st.success("Profilbild gespeichert.")
                    st.rerun()

        if user.get("avatar"):
            if st.button("Profilbild entfernen"):
                update_avatar(st.session_state.user_id, b"")
                st.success("Profilbild entfernt.")
                st.rerun()

    # --- Tab: Passwort ändern ---
    with tab_password:
        with st.form("change_pw_form"):
            current_pw = st.text_input("Aktuelles Passwort", type="password")
            new_pw = st.text_input("Neues Passwort", type="password")
            new_pw2 = st.text_input("Neues Passwort bestätigen", type="password")
            submitted = st.form_submit_button("Passwort ändern", use_container_width=True)

        if submitted:
            if not all([current_pw, new_pw, new_pw2]):
                st.error("Bitte alle Felder ausfüllen.")
            elif new_pw != new_pw2:
                st.error("Neue Passwörter stimmen nicht überein.")
            elif len(new_pw) < 8:
                st.error("Passwort muss mindestens 8 Zeichen lang sein.")
            else:
                ok = update_password(st.session_state.user_id, current_pw, new_pw)
                if ok:
                    st.success("Passwort erfolgreich geändert.")
                else:
                    st.error("Das aktuelle Passwort ist falsch.")

    # --- Tab: Account löschen ---
    with tab_delete:
        st.warning("Das Löschen deines Accounts ist dauerhaft und kann nicht rückgängig gemacht werden.")
        with st.form("delete_account_form"):
            confirm_pw = st.text_input("Passwort zur Bestätigung", type="password")
            confirm_check = st.checkbox("Ich möchte meinen Account dauerhaft löschen.")
            submitted = st.form_submit_button("Account löschen", use_container_width=True, type="primary")

        if submitted:
            if not confirm_pw:
                st.error("Bitte Passwort eingeben.")
            elif not confirm_check:
                st.error("Bitte die Checkbox bestätigen.")
            else:
                user_check = verify_login(user["username"], confirm_pw)
                if not user_check:
                    st.error("Falsches Passwort.")
                else:
                    delete_user(st.session_state.user_id)
                    st.session_state.logged_in = False
                    st.session_state.username = None
                    st.session_state.user_id = None
                    st.session_state.show_profile = False
                    st.success("Account wurde gelöscht.")
                    st.rerun()


# --- Main app ---
def show_main_page():
    with st.sidebar:
        user = get_user(st.session_state.user_id)
        if user and user.get("avatar"):
            img_bytes = base64.b64decode(user["avatar"])
            st.image(img_bytes, width=60)
        else:
            st.markdown("### 👤")
        st.write(f"Eingeloggt als **{st.session_state.username}**")
        if st.button("Mein Profil", use_container_width=True):
            st.session_state.show_profile = True
            st.rerun()
        if st.button("Ausloggen", use_container_width=True):
            st.session_state.logged_in = False
            st.session_state.username = None
            st.session_state.user_id = None
            st.session_state.show_profile = False
            st.rerun()

    if st.session_state.show_profile:
        show_profile_page()
    else:
        st.title(f"Willkommen, {st.session_state.username}! 👋")
        st.info("Dashboard folgt in den nächsten Sprints.")


# --- Router ---
reset_token = st.query_params.get("reset_token")
if reset_token:
    show_reset_password_page(reset_token)
elif st.session_state.logged_in:
    show_main_page()
else:
    show_auth_page()
