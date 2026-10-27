# RandPunkt — Estado del proyecto (traspaso entre sesiones)

## 1. Contexto general
Sistema editorial **RandPunkt Journal**.
- Web: ~/POLANCO/RANDPUNKT/web (FastAPI, puerto 8000)
- Editores: ~/POLANCO/EDITORIAL (Streamlit reviewers, puerto 8501)
- Coautores: ~/POLANCO/EDITORIAL (Streamlit coauthors, puerto 8502)
- Servidor SFTP: 187.217.52.137:3792 (user POLANCO6)

## 2. Credenciales
- polanco / polanco (Chief, polanco@unam.mx)
- luis / luis (Associate, cpg7000cpg@gmail.com)
- sujeto / sujeto (Associate, cpg7000cpg@gmail.com)
- SMTP configurado en secrets.toml (Gmail, randpunkt@gmail.com)
- base_url: http://192.168.1.80:8502

## 3. Estados del sistema
Flujo: en_revision -> todos_aprobados -> published (o retirado)
- en_revision -> todos_aprobados: Requiere arbitraje_confirmado=1 Y que todos firmen.
- todos_aprobados -> published: Solo el Chief. Simula Zenodo.
- Associate (luis) solo confirma arbitraje. Chief hace todo.

## 4. Tablas (CSV en /home/POLANCO6/RANDPUNKTJOURNAL/editorial/)
registro_articulos.csv: articulo_id, tipo, titulo, ruta_pdf, ruta_cover, fecha_creacion, estado, isbn, motivo_retiro, fecha_retiro, idioma_original, formato_original, editor_asignado, arbitraje_confirmado
registro_confirmaciones.csv: articulo_id, nombre, email, token, aprobado, fecha_aprobacion, comentarios

## 5. Archivos Clave (Streamlit en ~/POLANCO/EDITORIAL)
- reviewers.py: Panel de gestión editorial.
- coauthors.py: Panel de coautores.
- workflow.py: Lógica central de estados.
- auth_utils.py: Autenticación.
- seeds/: Scripts para crear datos de prueba.
- tests/: 21 tests (Playwright + requests).

## 6. Arrancar servicios
cd ~/POLANCO/EDITORIAL
nohup streamlit run reviewers.py --server.port 8501 > /tmp/reviewers.log 2>&1 &
nohup streamlit run coauthors.py --server.port 8502 > /tmp/coauthors.log 2>&1 &

cd ~/POLANCO/RANDPUNKT/web
nohup uvicorn app:app --host 0.0.0.0 --port 8000 > /tmp/app.log 2>&1 &

## 7. Seeds (datos de prueba)
python3 -m seeds.seed_all
python3 -m seeds.seed_reset_all
python3 -m seeds.seed_test_book
python3 -m seeds.seed_test_book_chief
python3 -m seeds.seed_test_book_published
python3 -m seeds.seed_test_article_formats

## 8. Tests (pytest)
cd ~/POLANCO/EDITORIAL
# Fase 1 (11 tests)
pytest tests/test_books_flow.py tests/test_books_coauthors.py tests/test_public_submission.py tests/test_email_flow.py::test_send_approval_email -v
# Fase 2 (4 tests)
pytest tests/test_article_formats.py -v
# Público (6 tests)
pytest tests/test_books_public.py -v

## 9. Estado actual
✅ COMPLETADO:
- 3 estados minimalistas + retirado.
- workflow.py unificado.
- reviewers.py minimalista y coauthors.py minimalista (busca por token, no email).
- auto_send_all en coauthors.py.
- Firma múltiple (mismo email, distinto token) funciona.
- SMTP verificado, correos a cpg7000cpg@gmail.com.
- Fase 1: 11/11 tests PASSED.
- Fase 2: 4/4 tests PASSED (Short Note, Scoped Expository, Research, Book).
- Público: 6/6 tests PASSED (listado, detalle, PDF, cover, 404).
- TOTAL: 21/21 tests PASSED.

⏳ PENDIENTE (opcional):
1. Correr test_wait_for_manual_signature (firma manual desde celular).
2. Configurar STORAGE_MODE=remote en producción (ahora está en local).
3. Añadir tests de artículos públicos (/articles, /articles/{id}).

## 10. Notas
- Streamlit no comparte sesión entre :8501 y :8502.
- st.rerun() desmonta el DOM. Test debe recargar página y re-loguear.
- Checkboxes ocultos: usar label:has(input[aria-label="..."]) con .click(force=True).
- Al resetear, parar Streamlit primero para evitar escrituras concurrentes.
- Zenodo: el CSV se actualiza antes que el JSON. El test debe hacer polling al JSON.
- Books públicos leen de uploads/ local (STORAGE_MODE=local). Si cambias a remote, ajustar.
