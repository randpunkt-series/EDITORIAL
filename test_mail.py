import streamlit as st
import smtplib
from email.mime.text import MIMEText

st.set_page_config(page_title="Test SMTP", page_icon="📧")
st.title("📧 Diagnóstico de Correo (SMTP)")

st.write("Este script probará la conexión con Gmail usando tus Secrets de Streamlit.")

if st.button("Ejecutar Prueba de Envío", type="primary"):
    try:
        # 1. Leer Secrets
        email_conf = st.secrets.get("email", {})
        smtp_server = email_conf.get("smtp_server", "smtp.gmail.com")
        smtp_port = int(email_conf.get("smtp_port", 587))
        sender_email = email_conf.get("gmail", "")
        sender_password = email_conf.get("password", "")

        st.info(f"**Servidor:** {smtp_server}:{smtp_port}")
        st.info(f"**Remitente:** {sender_email}")
        
        if not sender_password:
            st.error("❌ ERROR: No se encontró la contraseña en los Secrets (`[email] password`).")
            st.stop()
            
        st.success("✅ Secrets cargados correctamente.")

        # 2. Crear mensaje de prueba
        msg = MIMEText("Este es un correo de prueba del sistema RandPunkt para verificar la conexión SMTP.")
        msg['Subject'] = "Prueba de conexión RandPunkt"
        msg['From'] = sender_email
        msg['To'] = sender_email  # Te lo envías a ti mismo para probar

        # 3. Conectar al servidor
        st.write("⏳ Conectando al servidor SMTP...")
        server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
        server.ehlo()
        
        st.write("🔒 Iniciando TLS...")
        server.starttls()
        server.ehlo()

        # 4. Intentar Login
        st.write("🔑 Intentando iniciar sesión...")
        server.login(sender_email, sender_password)
        
        # 5. Enviar correo
        st.write("📤 Enviando correo de prueba...")
        server.send_message(msg)
        server.quit()
        
        st.balloons()
        st.success(f"🎉 ¡ÉXITO! El correo de prueba fue enviado a {sender_email}. Revisa tu bandeja de entrada (y Spam).")

    except smtplib.SMTPAuthenticationError as e:
        st.error("❌ ERROR DE AUTENTICACIÓN (535)")
        st.write("Gmail rechazó tu usuario y contraseña. **No estás usando una Contraseña de Aplicación válida**.")
        st.write("Debes ir a tu cuenta de Google -> Seguridad -> Verificación en 2 pasos -> Contraseñas de aplicaciones.")
        st.code(str(e))
    except (smtplib.SMTPConnectError, ConnectionRefusedError, TimeoutError) as e:
        st.error("❌ ERROR DE CONEXIÓN (Timeout / Firewall)")
        st.write("Streamlit Cloud está bloqueando el puerto 587. No se puede llegar a Gmail.")
        st.code(str(e))
    except Exception as e:
        st.error(f"❌ ERROR DESCONOCIDO: {type(e).__name__}")
        st.code(str(e))
