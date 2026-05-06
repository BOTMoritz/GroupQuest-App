import streamlit as st



def login_seite():
    st.title("Willkommen in der Groupquest-App!")

    username = st.text_input("Benutzername")
    passwort = st.text_input("Passwort", type="password")

    if st.button("Login"):
        if check_login(username, passwort):
            st.success("Erfolgreich eingeloggt!")
        else:
            st.error("Ungültige Anmeldedaten. Bitte versuchen Sie es erneut.")

def check_login(username, passwort):
    return username == "admin" and passwort == "password"




def logout():
    if st.button("Logout"):
        st.success("Erfolgreich ausgeloggt!")

if __name__ == "__main__":
    login_seite()
    logout()