import streamlit as st
import pymysql
from database import init_db, create_user, verify_login

st.set_page_config(page_title="GroupQuest", page_icon="🏆", layout="centered")

init_db()

# --- Session state defaults ---
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "username" not in st.session_state:
    st.session_state.username = None
if "user_id" not in st.session_state:
    st.session_state.user_id = None


# --- Auth page ---
def show_auth_page():
    st.title("GroupQuest 🏆")

    tab_login, tab_register = st.tabs(["Einloggen", "Account erstellen"])

    with tab_login:
        with st.form("login_form"):
            username = st.text_input("Benutzername")
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
                    st.success("Account erstellt! Du kannst dich jetzt einloggen.")
                except pymysql.IntegrityError:
                    st.error("Benutzername oder E-Mail ist bereits vergeben.")


# --- Main app (placeholder until dashboard is built) ---
def show_main_page():
    with st.sidebar:
        st.write(f"Eingeloggt als **{st.session_state.username}**")
        if st.button("Ausloggen"):
            st.session_state.logged_in = False
            st.session_state.username = None
            st.session_state.user_id = None
            st.rerun()

    st.title(f"Willkommen, {st.session_state.username}! 👋")
    st.info("Dashboard folgt in den nächsten Sprints.")


# --- Router ---
if st.session_state.logged_in:
    show_main_page()
else:
    show_auth_page()
