import streamlit as st
import os
import re
import json
import psycopg2
from psycopg2 import pool as pg_pool
from pathlib import Path
from datetime import date, datetime, timedelta
import pandas as pd
import io
import hashlib

try:
    from supabase import create_client, Client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, KeepTogether
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

APP_DIR = Path(__file__).resolve().parent

# ============================================================
# CONFIGURATION SUPABASE / POSTGRESQL
# ============================================================
# Les secrets sont lus depuis st.secrets (Streamlit Cloud) ou
# les variables d'environnement. Aucun mot de passe n'est écrit
# dans le code source.
DATABASE_URL = st.secrets.get("SUPABASE_DB_URL", os.getenv("SUPABASE_DB_URL", "")).strip()

SUPABASE_URL = st.secrets.get("SUPABASE_URL", os.getenv("SUPABASE_URL", "")).strip()
SUPABASE_SERVICE_ROLE_KEY = st.secrets.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
).strip()
if not SUPABASE_SERVICE_ROLE_KEY:
    SUPABASE_SERVICE_ROLE_KEY = st.secrets.get(
        "SUPABASE_KEY", os.getenv("SUPABASE_KEY", "")
    ).strip()
SUPABASE_BUCKET = st.secrets.get(
    "SUPABASE_STORAGE_BUCKET",
    os.getenv("SUPABASE_STORAGE_BUCKET", "cra-isra-files")
).strip()

DB_POOL = None
SUPABASE_CLIENT = None

def get_database_url():
    """Construit l'URL PostgreSQL depuis le secret complet ou les paramètres séparés."""
    if DATABASE_URL:
        return DATABASE_URL
    host = st.secrets.get("PGHOST", os.getenv("PGHOST", "")).strip()
    port = st.secrets.get("PGPORT", os.getenv("PGPORT", "5432")).strip()
    database = st.secrets.get("PGDATABASE", os.getenv("PGDATABASE", "postgres")).strip()
    user = st.secrets.get("PGUSER", os.getenv("PGUSER", "")).strip()
    password = st.secrets.get("PGPASSWORD", os.getenv("PGPASSWORD", "")).strip()
    if all([host, port, database, user, password]):
        from urllib.parse import quote_plus
        return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{database}"
    return ""

def init_postgres_pool():
    global DB_POOL
    url = get_database_url()
    if not url:
        raise RuntimeError(
            "SUPABASE_DB_URL est absent. Ajoutez-le dans Streamlit Cloud > Settings > Secrets."
        )
    if DB_POOL is None:
        DB_POOL = pg_pool.ThreadedConnectionPool(1, 8, dsn=url, connect_timeout=10)
    return DB_POOL

def get_conn():
    pool = init_postgres_pool()
    conn = pool.getconn()
    conn.autocommit = False
    return conn

def put_conn(conn):
    if DB_POOL is not None and conn is not None:
        DB_POOL.putconn(conn)

def init_supabase_storage():
    global SUPABASE_CLIENT
    if not SUPABASE_AVAILABLE or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    if SUPABASE_CLIENT is None:
        SUPABASE_CLIENT = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return SUPABASE_CLIENT

def ensure_storage_bucket():
    """Crée le bucket privé si le client service-role est disponible."""
    client = init_supabase_storage()
    if client is None:
        return False
    try:
        buckets = client.storage.list_buckets()
        names = {getattr(b, "name", None) or (b.get("name") if isinstance(b, dict) else None) for b in buckets}
        if SUPABASE_BUCKET not in names:
            client.storage.create_bucket(SUPABASE_BUCKET, {"public": False})
        return True
    except Exception:
        # Le bucket peut déjà exister ou la clé peut ne pas avoir le droit
        # de créer des buckets. L'upload sera tenté normalement.
        return True

def upload_to_supabase(uploaded, subdir=""):
    """Envoie le fichier lourd vers Supabase Storage et retourne son chemin."""
    if uploaded is None:
        return ""
    client = init_supabase_storage()
    if client is None:
        raise RuntimeError(
            "Supabase Storage n'est pas configuré. Ajoutez SUPABASE_URL et "
            "SUPABASE_SERVICE_ROLE_KEY dans les Secrets."
        )
    ensure_storage_bucket()
    original = Path(uploaded.name).name
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", original).strip("._") or "fichier"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    storage_path = f"{subdir.strip('/') + '/' if subdir else ''}{stamp}_{safe}"
    data = uploaded.getvalue()
    options = {"content-type": uploaded.type or "application/octet-stream", "upsert": "false"}
    client.storage.from_(SUPABASE_BUCKET).upload(storage_path, data, options)
    return f"supabase://{SUPABASE_BUCKET}/{storage_path}"

def storage_download_url(storage_ref, expires_in=3600):
    """Génère une URL signée temporaire pour un fichier privé."""
    if not storage_ref or not storage_ref.startswith("supabase://"):
        return None
    client = init_supabase_storage()
    if client is None:
        return None
    try:
        _, rest = storage_ref.split("supabase://", 1)
        bucket, path = rest.split("/", 1)
        result = client.storage.from_(bucket).create_signed_url(path, expires_in)
        if isinstance(result, dict):
            return result.get("signedURL") or result.get("signedUrl")
        return getattr(result, "signed_url", None) or getattr(result, "signedURL", None)
    except Exception:
        return None

# -----------------------------
# AUTHENTIFICATION
# -----------------------------
# Identifiants demandés pour l'accès à l'application.
APP_USERNAME = "Ndeye Fota Gueye"
APP_PASSWORD = "nfg2026"


st.set_page_config(
    page_title="Registre CRA/ISRA",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# -----------------------------
# STYLE
# -----------------------------
st.markdown("""
<style>
.stApp { background: linear-gradient(135deg,#f5f8fc 0%,#eef7f1 50%,#f7fbff 100%); }
header[data-testid="stHeader"] { background: #0b4f8a; }
.block-container { padding-top: 1.2rem; max-width: 1450px; }
.hero { animation: fadeInDown .7s ease-out;
    background: linear-gradient(135deg,#0b4f8a,#1677c8);
    color:white; padding:28px; border-radius:18px; margin-bottom:20px;
}
.hero h1 { margin:0; font-size:2rem; }\n.hero-badge { display:inline-block; margin-bottom:10px; padding:5px 12px; border-radius:999px; background:rgba(255,255,255,.16); font-size:.78rem; letter-spacing:.08em; text-transform:uppercase; }\n.login-card { max-width:520px; margin:8vh auto; background:white; padding:32px; border-radius:24px; box-shadow:0 12px 45px rgba(20,60,100,.16); border:1px solid #dfeaf3; animation:fadeInUp .6s ease-out; }\n.brand-strip { display:flex; gap:10px; align-items:center; justify-content:center; margin-bottom:16px; }\n.brand-dot { width:12px; height:12px; border-radius:50%; background:#2d8a4a; box-shadow:0 0 0 5px rgba(45,138,74,.12); animation:pulse 2s infinite; }\n@keyframes fadeInDown { from {opacity:0; transform:translateY(-14px)} to {opacity:1; transform:translateY(0)} }\n@keyframes fadeInUp { from {opacity:0; transform:translateY(18px)} to {opacity:1; transform:translateY(0)} }\n@keyframes pulse { 0%,100% {transform:scale(1)} 50% {transform:scale(1.25)} }
.hero p { margin:8px 0 0; opacity:.92; }
.card {
    background:white; padding:18px; border-radius:15px;
    box-shadow:0 3px 14px rgba(20,60,100,.08);
    border:1px solid #e6edf5; margin-bottom:14px;
}
.metric {
    background:white; border:1px solid #e6edf5; border-radius:15px;
    padding:18px; box-shadow:0 2px 10px rgba(20,60,100,.06);
}
.metric .label { color:#64748b; font-size:.85rem; }
.metric .value { color:#0b4f8a; font-size:1.65rem; font-weight:700; }
h2,h3 { color:#0b4f8a; }
div[data-testid="stForm"] { background:white; padding:20px; border-radius:15px; border:1px solid #e6edf5; }
</style>
""", unsafe_allow_html=True)

# ============================================================
# MASQUAGE DE L'INTERFACE TECHNIQUE STREAMLIT / GITHUB
# ============================================================
# Ce bloc doit impérativement être envoyé à Streamlit avec
# st.markdown(..., unsafe_allow_html=True).
st.markdown("""
<style>
/* Menu principal et footer Streamlit */
#MainMenu,
footer {
    display: none !important;
    visibility: hidden !important;
}

/* Barre d'outils, décoration, statut et bouton Deploy */
div[data-testid="stToolbar"],
div[data-testid="stDecoration"],
div[data-testid="stStatusWidget"],
div[data-testid="stAppDeployButton"],
div[data-testid="stHeaderActionElements"],
[data-testid="stToolbar"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
[data-testid="stAppDeployButton"],
[data-testid="stHeaderActionElements"] {
    display: none !important;
    visibility: hidden !important;
}

/* Liens et boutons GitHub / Deploy dans l'en-tête */
header[data-testid="stHeader"] a[href*="github.com"],
header[data-testid="stHeader"] a[href*="streamlit.io"],
header[data-testid="stHeader"] button[aria-label*="GitHub"],
header[data-testid="stHeader"] button[title*="GitHub"],
header[data-testid="stHeader"] button[aria-label*="Deploy"],
header[data-testid="stHeader"] button[title*="Deploy"],
header[data-testid="stHeader"] [data-testid*="GitHub"],
header[data-testid="stHeader"] [data-testid*="github"],
header[data-testid="stHeader"] [data-testid*="Deploy"],
header[data-testid="stHeader"] [data-testid*="deploy"] {
    display: none !important;
    visibility: hidden !important;
}

/* Réduit l'espace visuel de l'en-tête technique */
header[data-testid="stHeader"] {
    min-height: 0 !important;
}

/* Éléments techniques susceptibles d'apparaître en bas */
div[data-testid="stBottom"],
div[data-testid="stBottomBlockContainer"] {
    display: none !important;
    visibility: hidden !important;
}
</style>
""", unsafe_allow_html=True)


# -----------------------------
# AUTHENTIFICATION UI
# -----------------------------
def show_login():
    st.markdown("""
    <div class="login-card">
      <div class="brand-strip"><span class="brand-dot"></span><strong>ISRA • CRA</strong><span class="brand-dot"></span></div>
      <h1 style="text-align:center;color:#0b4f8a;margin-bottom:4px;">📚 Registre CRA/ISRA</h1>
      <p style="text-align:center;color:#64748b;">Communication • Documentation • IST • Bibliothèque</p>
    </div>
    """, unsafe_allow_html=True)
    with st.form("login_form", clear_on_submit=False):
        username = st.text_input("Nom d'utilisateur", placeholder="Ndeye Fota Gueye")
        password = st.text_input("Mot de passe", type="password", placeholder="••••••••")
        submitted = st.form_submit_button("🔐 Se connecter", type="primary", use_container_width=True)
    if submitted:
        if username.strip() == APP_USERNAME and password == APP_PASSWORD:
            st.session_state["authenticated"] = True
            st.session_state["logged_user"] = APP_USERNAME
            st.rerun()
        else:
            st.error("Nom d'utilisateur ou mot de passe incorrect.")
    st.caption("Accès interne — CRA/ISRA")

if not st.session_state.get("authenticated", False):
    show_login()
    st.stop()


# -----------------------------
# DATABASE POSTGRESQL
# -----------------------------
def _sql(sql):
    # Le code historique utilisait des placeholders SQLite (?) ;
    # PostgreSQL/psycopg2 utilise %s.
    return sql.replace("?", "%s")

def init_db():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS people (
            id BIGSERIAL PRIMARY KEY,
            nom TEXT NOT NULL,
            prenom TEXT,
            fonction TEXT,
            structure TEXT,
            categorie TEXT,
            telephone TEXT,
            email TEXT,
            localite TEXT,
            region TEXT,
            observations TEXT,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS activities (
            id BIGSERIAL PRIMARY KEY,
            reference TEXT UNIQUE,
            titre TEXT NOT NULL,
            type_activite TEXT,
            domaine TEXT,
            date_activite DATE,
            heure_debut TEXT,
            heure_fin TEXT,
            lieu TEXT,
            region TEXT,
            localite TEXT,
            responsable TEXT,
            description TEXT,
            objectifs TEXT,
            resultats TEXT,
            observations TEXT,
            statut TEXT DEFAULT 'Prévue',
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS participants (
            id BIGSERIAL PRIMARY KEY,
            activity_id BIGINT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
            person_id BIGINT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            present BOOLEAN DEFAULT TRUE,
            observations TEXT
        );
        CREATE TABLE IF NOT EXISTS communications (
            id BIGSERIAL PRIMARY KEY,
            activity_id BIGINT REFERENCES activities(id) ON DELETE SET NULL,
            titre TEXT NOT NULL,
            type_action TEXT,
            plateforme TEXT,
            date_publication DATE,
            lien TEXT,
            vues BIGINT DEFAULT 0,
            reactions BIGINT DEFAULT 0,
            commentaires BIGINT DEFAULT 0,
            partages BIGINT DEFAULT 0,
            telechargements BIGINT DEFAULT 0,
            observations TEXT
        );
        CREATE TABLE IF NOT EXISTS media (
            id BIGSERIAL PRIMARY KEY,
            activity_id BIGINT REFERENCES activities(id) ON DELETE SET NULL,
            media_name TEXT NOT NULL,
            media_type TEXT,
            journaliste TEXT,
            personne_interviewee TEXT,
            sujet TEXT,
            date_intervention DATE,
            lieu TEXT,
            type_intervention TEXT,
            lien TEXT,
            observations TEXT
        );
        CREATE TABLE IF NOT EXISTS audiovisual (
            id BIGSERIAL PRIMARY KEY,
            activity_id BIGINT REFERENCES activities(id) ON DELETE SET NULL,
            titre TEXT NOT NULL,
            type_production TEXT,
            date_production DATE,
            lieu TEXT,
            theme TEXT,
            interviewes TEXT,
            duree TEXT,
            responsable TEXT,
            statut TEXT,
            lien TEXT,
            fichier_original TEXT,
            observations TEXT
        );
        CREATE TABLE IF NOT EXISTS documents (
            id BIGSERIAL PRIMARY KEY,
            titre TEXT NOT NULL,
            auteurs TEXT,
            annee INTEGER,
            type_document TEXT,
            thematique TEXT,
            mots_cles TEXT,
            chercheur_associe TEXT,
            projet TEXT,
            resume TEXT,
            langue TEXT,
            pages INTEGER,
            reference TEXT,
            fichier TEXT,
            lien TEXT,
            statut TEXT,
            observations TEXT
        );
        CREATE TABLE IF NOT EXISTS library_visits (
            id BIGSERIAL PRIMARY KEY,
            person_id BIGINT REFERENCES people(id) ON DELETE SET NULL,
            date_visite DATE NOT NULL,
            heure_arrivee TEXT,
            heure_depart TEXT,
            motif TEXT,
            documents_consultes TEXT,
            documents_empruntes TEXT,
            observations TEXT
        );
        CREATE TABLE IF NOT EXISTS library_items (
            id BIGSERIAL PRIMARY KEY,
            inventaire TEXT UNIQUE,
            cote TEXT,
            isbn TEXT,
            titre TEXT NOT NULL,
            sous_titre TEXT,
            auteurs TEXT,
            editeur TEXT,
            annee INTEGER,
            type_document TEXT,
            domaine TEXT,
            thematique TEXT,
            mots_cles TEXT,
            exemplaires INTEGER DEFAULT 1,
            disponibles INTEGER DEFAULT 1,
            localisation TEXT,
            etat TEXT,
            format_document TEXT,
            resume TEXT,
            fichier TEXT,
            lien TEXT,
            observations TEXT
        );
        CREATE TABLE IF NOT EXISTS consultations (
            id BIGSERIAL PRIMARY KEY,
            person_id BIGINT REFERENCES people(id) ON DELETE SET NULL,
            item_id BIGINT REFERENCES library_items(id) ON DELETE SET NULL,
            date_consultation DATE NOT NULL,
            heure TEXT,
            type_consultation TEXT,
            observations TEXT
        );
        CREATE TABLE IF NOT EXISTS loans (
            id BIGSERIAL PRIMARY KEY,
            person_id BIGINT REFERENCES people(id) ON DELETE SET NULL,
            item_id BIGINT REFERENCES library_items(id) ON DELETE SET NULL,
            date_emprunt DATE NOT NULL,
            date_retour_prevue DATE NOT NULL,
            date_retour_reelle DATE,
            statut TEXT DEFAULT 'En cours',
            observations TEXT
        );
        CREATE TABLE IF NOT EXISTS researcher_valorization (
            id BIGSERIAL PRIMARY KEY,
            person_id BIGINT REFERENCES people(id) ON DELETE SET NULL,
            date_action DATE,
            domaine TEXT,
            thematique TEXT,
            projet TEXT,
            type_valorisation TEXT,
            support TEXT,
            lien TEXT,
            observations TEXT
        );
        CREATE TABLE IF NOT EXISTS evidence (
            id BIGSERIAL PRIMARY KEY,
            activity_id BIGINT REFERENCES activities(id) ON DELETE CASCADE,
            nom_fichier TEXT NOT NULL,
            chemin TEXT,
            type_fichier TEXT,
            date_ajout TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date_activite);
        CREATE INDEX IF NOT EXISTS idx_documents_year ON documents(annee);
        CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(statut);
        CREATE INDEX IF NOT EXISTS idx_visits_date ON library_visits(date_visite);
        CREATE INDEX IF NOT EXISTS idx_storage_audiovisual ON audiovisual(fichier_original);
        CREATE INDEX IF NOT EXISTS idx_storage_documents ON documents(fichier);
        CREATE INDEX IF NOT EXISTS idx_storage_library ON library_items(fichier);
        """)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        put_conn(conn)

init_db()

# -----------------------------
# HELPERS
# -----------------------------
def q(sql, params=(), fetch=False):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(_sql(sql), params)
        if fetch:
            rows = cur.fetchall()
            columns = [d[0] for d in cur.description] if cur.description else []
            return [dict(zip(columns, row)) for row in rows]
        last = None
        if cur.description and cur.description[0][0] == "id":
            row = cur.fetchone()
            last = row[0] if row else None
        conn.commit()
        return last
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        put_conn(conn)

def df(sql, params=()):
    return pd.DataFrame(q(sql, params, True))

def scalar(sql, params=()):
    rows = q(sql, params, True)
    return list(rows[0].values())[0] if rows else 0

def options_people():
    rows = q("SELECT id, nom, prenom, structure FROM people ORDER BY nom, prenom", fetch=True)
    return {f"{r['nom']} {r['prenom'] or ''} — {r['structure'] or ''}".strip(): r["id"] for r in rows}

def options_items():
    rows = q("SELECT id, inventaire, titre, disponibles FROM library_items ORDER BY titre", fetch=True)
    return {f"{r['titre']} [{r['inventaire'] or r['id']}] — dispo: {r['disponibles']}": r["id"] for r in rows}

def save_uploaded(uploaded, subdir=""):
    if not uploaded:
        return ""
    return upload_to_supabase(uploaded, subdir)

def excel_bytes(dataframes):
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        for name, data in dataframes.items():
            data.to_excel(writer, sheet_name=name[:31], index=False)
    return bio.getvalue()


def csv_bytes(data):
    return data.to_csv(index=False).encode("utf-8-sig")


def _pdf_header_footer(canvas, doc):
    """Habillage A4 premium : en-tête, ligne graphique, pied de page."""
    canvas.saveState()
    w, h = A4
    navy = colors.HexColor("#083B66")
    blue = colors.HexColor("#0B74B8")
    green = colors.HexColor("#2D8A4A")
    light = colors.HexColor("#EAF4FA")
    # Bandeau haut
    canvas.setFillColor(navy)
    canvas.rect(0, h-58, w, 58, fill=1, stroke=0)
    canvas.setFillColor(green)
    canvas.rect(0, h-63, w, 5, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(34, h-32, "ISRA • CRA")
    canvas.setFont("Helvetica", 7.5)
    canvas.drawRightString(w-34, h-32, "COMMUNICATION • DOCUMENTATION • IST • BIBLIOTHÈQUE")
    # Pied
    canvas.setStrokeColor(colors.HexColor("#D7E5EF"))
    canvas.line(34, 31, w-34, 31)
    canvas.setFillColor(colors.HexColor("#64748B"))
    canvas.setFont("Helvetica", 7)
    canvas.drawString(34, 19, "Registre interne CRA/ISRA • Ndeye Fota Gueye")
    canvas.drawRightString(w-34, 19, f"Page {doc.page}")
    canvas.restoreState()


def _safe_para(value, style):
    value = "" if value is None else str(value)
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    value = value.replace("\n", "<br/>")
    return Paragraph(value, style)


def _df_table(data, body_style, head_style):
    """Tableau A4 lisible, sans supprimer les colonnes ni les données."""
    d = data.copy().fillna("")
    headers = [str(c) for c in d.columns]
    table_data = [[_safe_para(c, head_style) for c in headers]]
    for row in d.astype(str).values.tolist():
        table_data.append([_safe_para(v, body_style) for v in row])
    # A4 portrait : largeur utile 523 pt, colonnes pondérées par le contenu
    usable = 523
    weights = []
    for col in d.columns:
        max_len = max([len(str(col))] + [len(str(x)) for x in d[col].head(120)])
        weights.append(min(max(max_len * 3.0, 42), 145))
    total = sum(weights) or 1
    widths = [usable * x / total for x in weights]
    tbl = Table(table_data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#083B66")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 7),
        ("FONTSIZE", (0,1), (-1,-1), 6.1),
        ("GRID", (0,0), (-1,-1), .25, colors.HexColor("#C7D8E5")),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#F5F9FC")]),
        ("LEFTPADDING", (0,0), (-1,-1), 4),
        ("RIGHTPADDING", (0,0), (-1,-1), 4),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))
    return tbl


def pdf_bytes(data, title="Rapport CRA/ISRA"):
    """PDF A4 portrait premium pour un registre donné."""
    if not REPORTLAB_AVAILABLE:
        return None
    bio = io.BytesIO()
    doc = SimpleDocTemplate(
        bio, pagesize=A4, rightMargin=34, leftMargin=34, topMargin=78, bottomMargin=42
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "BrandTitle", parent=styles["Title"], alignment=TA_LEFT,
        fontSize=20, leading=24, textColor=colors.HexColor("#083B66"),
        spaceAfter=7
    )
    subtitle = ParagraphStyle(
        "Subtitle", parent=styles["BodyText"], fontSize=8.5, leading=12,
        textColor=colors.HexColor("#64748B"), spaceAfter=12
    )
    body = ParagraphStyle("PDFBody", parent=styles["BodyText"], fontSize=8, leading=10)
    head = ParagraphStyle("PDFHead", parent=body, textColor=colors.white,
                          fontName="Helvetica-Bold", fontSize=7, leading=8)
    section = ParagraphStyle(
        "Section", parent=styles["Heading2"], fontSize=12, leading=15,
        textColor=colors.HexColor("#0B74B8"), spaceBefore=8, spaceAfter=7
    )
    story = [
        Paragraph("RAPPORT • CRA / ISRA", title_style),
        Paragraph(title, section),
        Paragraph(
            f"Profil : <b>Ndeye Fota Gueye</b> — Chargée de la communication et documentation de CRA/ISRA Saint-Louis"
            f"<br/>Généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')}",
            subtitle
        ),
    ]
    if data is None or data.empty:
        story.append(Paragraph("Aucune donnée à afficher.", body))
    else:
        story.append(_df_table(data, body, head))
    doc.build(story, onFirstPage=_pdf_header_footer, onLaterPages=_pdf_header_footer)
    return bio.getvalue()


def complete_report_pdf(report_data, start=None, end=None):
    """Rapport global A4 : profil + indicateurs + toutes les tables renseignées dans le site."""
    if not REPORTLAB_AVAILABLE:
        return None
    bio = io.BytesIO()
    doc = SimpleDocTemplate(
        bio, pagesize=A4, rightMargin=34, leftMargin=34, topMargin=78, bottomMargin=42
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle("CRATitle", parent=styles["Title"], fontSize=22, leading=26,
                           textColor=colors.HexColor("#083B66"), spaceAfter=8)
    sub = ParagraphStyle("CRASub", parent=styles["BodyText"], fontSize=9, leading=13,
                         textColor=colors.HexColor("#475569"), spaceAfter=10)
    sec = ParagraphStyle("CRASection", parent=styles["Heading2"], fontSize=13, leading=16,
                         textColor=colors.HexColor("#0B74B8"), spaceBefore=10, spaceAfter=8)
    body = ParagraphStyle("CRABody", parent=styles["BodyText"], fontSize=8, leading=10)
    head = ParagraphStyle("CRAHead", parent=body, textColor=colors.white,
                          fontName="Helvetica-Bold", fontSize=7, leading=8)

    story = [
        Paragraph("DOSSIER COMPLET", title),
        Paragraph("Registre Communication • Documentation • IST • Bibliothèque", sec),
        Paragraph(
            "<b>Ndeye Fota Gueye</b><br/>"
            "Chargée de la communication et documentation de CRA/ISRA Saint-Louis<br/>"
            f"Période : {start or '—'} → {end or '—'} • Généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')}",
            sub
        ),
    ]

    # Synthèse calculée à partir des mêmes données que l'application
    story.append(Paragraph("01 — SYNTHÈSE", sec))
    metrics = []
    for label, frame in report_data.items():
        metrics.append([label, str(len(frame))])
    story.append(_df_table(pd.DataFrame(metrics, columns=["Registre", "Nombre d'enregistrements"]), body, head))

    for idx, (name, frame) in enumerate(report_data.items(), start=2):
        story.append(PageBreak())
        story.append(Paragraph(f"{idx:02d} — {name.upper()}", sec))
        if frame is None or frame.empty:
            story.append(Paragraph("Aucune donnée renseignée pour cette rubrique.", body))
        else:
            story.append(_df_table(frame, body, head))

    doc.build(story, onFirstPage=_pdf_header_footer, onLaterPages=_pdf_header_footer)
    return bio.getvalue()

def export_buttons(data, base_name, title):
    """Boutons CSV, Excel et PDF pour chaque registre/document affiché."""
    if data is None:
        return
    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button(
            "⬇️ CSV", csv_bytes(data), f"{base_name}.csv",
            "text/csv", key=f"csv_{base_name}"
        )
    with c2:
        st.download_button(
            "⬇️ Excel", excel_bytes({"Donnees": data}), f"{base_name}.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"xlsx_{base_name}"
        )
    with c3:
        pdf = pdf_bytes(data, title)
        if pdf is not None:
            st.download_button(
                "⬇️ PDF", pdf, f"{base_name}.pdf",
                "application/pdf", key=f"pdf_{base_name}"
            )
        else:
            st.caption("PDF indisponible : installez reportlab.")

# -----------------------------
# HEADER / NAV
# -----------------------------
st.markdown("""
<div class="hero">
<div class="hero-badge">ISRA • CRA • REGISTRE INTERNE</div>
<h1>📚 Registre Communication, Documentation, IST & Bibliothèque</h1>
<p>Outil interne de suivi, capitalisation, animation scientifique et production de rapports — session : Ndeye Fota Gueye</p>
</div>
""", unsafe_allow_html=True)
c_logout1, c_logout2 = st.columns([8,1])
with c_logout2:
    if st.button("🚪 Déconnexion", use_container_width=True):
        st.session_state.clear()
        st.rerun()

pages = [
    "🏠 Tableau de bord", "➕ Activités", "👥 Personnes", "📣 Communication",
    "📰 Médias", "🎥 Audiovisuel", "🔬 Valorisation chercheurs",
    "📄 Documentation / IST", "📚 Bibliothèque", "👤 Visiteurs",
    "📖 Consultations", "📕 Prêts / Retours", "👤 Profil", "📊 Rapports & exports"
]
page = st.radio("Navigation", pages, horizontal=True, label_visibility="collapsed")

# -----------------------------
# DASHBOARD
# -----------------------------
if page == "🏠 Tableau de bord":
    today = date.today().isoformat()
    metrics = [
        ("Activités", scalar("SELECT COUNT(*) FROM activities")),
        ("Participants", scalar("SELECT COUNT(*) FROM participants")),
        ("Personnes", scalar("SELECT COUNT(*) FROM people")),
        ("Publications/actions", scalar("SELECT COUNT(*) FROM communications")),
        ("Documents IST", scalar("SELECT COUNT(*) FROM documents")),
        ("Documents bibliothèque", scalar("SELECT COUNT(*) FROM library_items")),
        ("Visiteurs", scalar("SELECT COUNT(*) FROM library_visits")),
        ("Prêts en cours", scalar("SELECT COUNT(*) FROM loans WHERE statut='En cours'")),
        ("Prêts en retard", scalar("SELECT COUNT(*) FROM loans WHERE statut='En retard'")),
    ]
    cols = st.columns(3)
    for i, (label, value) in enumerate(metrics):
        with cols[i % 3]:
            st.markdown(f'<div class="metric"><div class="label">{label}</div><div class="value">{value}</div></div>', unsafe_allow_html=True)

    st.markdown("### 📈 Activités par mois")
    act = df("""
        SELECT substr(date_activite,1,7) AS mois, COUNT(*) AS nombre
        FROM activities GROUP BY mois ORDER BY mois
    """)
    if not act.empty:
        st.line_chart(act.set_index("mois"))
    else:
        st.info("Aucune activité enregistrée.")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("### 📚 Bibliothèque")
        lib = df("""
            SELECT type_document AS type, SUM(exemplaires) AS exemplaires,
                   SUM(disponibles) AS disponibles
            FROM library_items GROUP BY type_document ORDER BY exemplaires DESC
        """)
        st.dataframe(lib, use_container_width=True, hide_index=True)
        export_buttons(lib, "bibliotheque_dashboard", "Bibliothèque — synthèse CRA/ISRA")
    with c2:
        st.markdown("### ⏰ Prêts à surveiller")
        late = df("""
            SELECT l.id, p.nom || ' ' || COALESCE(p.prenom,'') AS emprunteur,
                   i.titre, l.date_emprunt, l.date_retour_prevue, l.statut
            FROM loans l
            LEFT JOIN people p ON p.id=l.person_id
            LEFT JOIN library_items i ON i.id=l.item_id
            WHERE l.statut IN ('En cours','En retard')
            ORDER BY l.date_retour_prevue
        """)
        st.dataframe(late, use_container_width=True, hide_index=True)
        export_buttons(late, "prets_a_surveiller", "Prêts à surveiller — CRA/ISRA")

# -----------------------------
# ACTIVITIES
# -----------------------------
elif page == "➕ Activités":
    st.header("➕ Enregistrer une activité")
    with st.form("activity_form"):
        c1, c2, c3 = st.columns(3)
        titre = c1.text_input("Titre *")
        type_a = c2.selectbox("Type", ["Animation scientifique","Atelier","Réunion","Formation","Sensibilisation","Conférence","Séminaire","Forum","Salon","Foire","Journée portes ouvertes","Visite","Mission","Rencontre partenaires","Communication","Documentation","Bibliothèque","Autre"])
        domaine = c3.text_input("Domaine")
        c1, c2, c3 = st.columns(3)
        d = c1.date_input("Date", date.today())
        hd = c2.text_input("Heure début")
        hf = c3.text_input("Heure fin")
        c1, c2, c3 = st.columns(3)
        lieu = c1.text_input("Lieu")
        region = c2.text_input("Région")
        localite = c3.text_input("Localité")
        responsable = st.text_input("Responsable")
        statut = st.selectbox("Statut", ["Prévue","En préparation","Réalisée","Reportée","Annulée"])
        objectifs = st.text_area("Objectifs")
        description = st.text_area("Description")
        resultats = st.text_area("Résultats obtenus")
        observations = st.text_area("Observations")
        submitted = st.form_submit_button("💾 Enregistrer", type="primary")
    if submitted:
        if not titre.strip():
            st.error("Le titre est obligatoire.")
        else:
            ref = f"ACT-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            q("""INSERT INTO activities(reference,titre,type_activite,domaine,date_activite,heure_debut,heure_fin,
                 lieu,region,localite,responsable,description,objectifs,resultats,observations,statut)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (ref,titre,type_a,domaine,str(d),hd,hf,lieu,region,localite,responsable,description,objectifs,resultats,observations,statut))
            st.success(f"Activité enregistrée : {ref}")
            st.rerun()

    st.divider()
    st.subheader("Activités enregistrées")
    data = df("SELECT * FROM activities ORDER BY date_activite DESC, id DESC")
    st.dataframe(data, use_container_width=True, hide_index=True)

# -----------------------------
# PEOPLE
# -----------------------------
elif page == "👥 Personnes":
    st.header("👥 Personnes, chercheurs, participants et partenaires")
    with st.form("person_form"):
        c1,c2,c3 = st.columns(3)
        nom=c1.text_input("Nom *"); prenom=c2.text_input("Prénom"); fonction=c3.text_input("Fonction")
        c1,c2,c3 = st.columns(3)
        structure=c1.text_input("Structure / institution"); categorie=c2.selectbox("Catégorie",["Chercheur","Enseignant-chercheur","Technicien","Personnel administratif","Personnel CRA/ISRA","Étudiant","Stagiaire","Producteur","Organisation de producteurs","Partenaire technique","Partenaire financier","Institution publique","Décideur","Journaliste","Média","ONG","Organisation professionnelle","Visiteur","Communauté locale","Jeune","Grand public","Autre"])
        localite=c3.text_input("Localité")
        c1,c2,c3=st.columns(3)
        telephone=c1.text_input("Téléphone"); email=c2.text_input("Email"); region=c3.text_input("Région")
        observations=st.text_area("Observations")
        ok=st.form_submit_button("💾 Ajouter la personne", type="primary")
    if ok:
        if not nom.strip(): st.error("Le nom est obligatoire.")
        else:
            q("""INSERT INTO people(nom,prenom,fonction,structure,categorie,telephone,email,localite,region,observations)
                 VALUES(?,?,?,?,?,?,?,?,?,?)""",(nom,prenom,fonction,structure,categorie,telephone,email,localite,region,observations))
            st.success("Personne ajoutée."); st.rerun()
    data=df("SELECT id,nom,prenom,fonction,structure,categorie,telephone,email,localite,region FROM people ORDER BY nom")
    st.dataframe(data,use_container_width=True,hide_index=True)
    export_buttons(data, "personnes", "Registre des personnes CRA/ISRA")

# -----------------------------
# COMMUNICATION
# -----------------------------
elif page == "📣 Communication":
    st.header("📣 Actions de communication et diffusion")
    acts=df("SELECT id,reference,titre FROM activities ORDER BY date_activite DESC")
    actmap={f"{r.reference} — {r.titre}":r.id for r in acts.itertuples()} if not acts.empty else {}
    with st.form("comm_form"):
        activity_label=st.selectbox("Activité liée (facultatif)",["—"]+list(actmap))
        c1,c2,c3=st.columns(3)
        titre=c1.text_input("Titre *"); type_action=c2.selectbox("Type d'action",["Affiche","Invitation","Communiqué","Dossier de presse","Article","Reportage","Interview","Photographie","Vidéo","Film","Brochure","Plaquette","Kakemono","Publication web","Autre"])
        plateforme=c3.selectbox("Plateforme",["Site web","Facebook","LinkedIn","YouTube","WhatsApp","Radio","Télévision","Presse écrite","Autre"])
        c1,c2,c3=st.columns(3)
        dp=c1.date_input("Date de publication",date.today()); lien=c2.text_input("Lien"); vues=c3.number_input("Vues",0,step=1)
        c1,c2,c3=st.columns(3)
        reactions=c1.number_input("Réactions",0,step=1); commentaires=c2.number_input("Commentaires",0,step=1); partages=c3.number_input("Partages",0,step=1)
        telechargements=st.number_input("Téléchargements",0,step=1)
        observations=st.text_area("Observations")
        ok=st.form_submit_button("💾 Enregistrer",type="primary")
    if ok:
        if not titre.strip(): st.error("Le titre est obligatoire.")
        else:
            aid=actmap.get(activity_label)
            q("""INSERT INTO communications(activity_id,titre,type_action,plateforme,date_publication,lien,vues,reactions,commentaires,partages,telechargements,observations)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(aid,titre,type_action,plateforme,str(dp),lien,vues,reactions,commentaires,partages,telechargements,observations))
            st.success("Action de communication enregistrée."); st.rerun()
    comm_data = df("SELECT * FROM communications ORDER BY date_publication DESC")
    st.dataframe(comm_data,use_container_width=True,hide_index=True)
    export_buttons(comm_data, "communication", "Actions de communication CRA/ISRA")

# -----------------------------
# MEDIA
# -----------------------------
elif page == "📰 Médias":
    st.header("📰 Presse et médias")
    with st.form("media_form"):
        c1,c2,c3=st.columns(3)
        media_name=c1.text_input("Nom du média *"); media_type=c2.selectbox("Type",["Télévision","Radio","Presse écrite","Presse en ligne","Magazine","Site web","Autre"]); journaliste=c3.text_input("Journaliste")
        c1,c2,c3=st.columns(3)
        interviewee=c1.text_input("Personne interviewée"); sujet=c2.text_input("Sujet"); di=c3.date_input("Date",date.today())
        lieu=st.text_input("Lieu"); type_intervention=st.selectbox("Type d'intervention",["Interview","Reportage","Article","Émission","Conférence de presse","Autre"]); lien=st.text_input("Lien")
        observations=st.text_area("Observations")
        ok=st.form_submit_button("💾 Enregistrer",type="primary")
    if ok:
        q("""INSERT INTO media(media_name,media_type,journaliste,personne_interviewee,sujet,date_intervention,lieu,type_intervention,lien,observations)
             VALUES(?,?,?,?,?,?,?,?,?,?)""",(media_name,media_type,journaliste,interviewee,sujet,str(di),lieu,type_intervention,lien,observations))
        st.success("Intervention média enregistrée."); st.rerun()
    media_data = df("SELECT * FROM media ORDER BY date_intervention DESC")
    st.dataframe(media_data,use_container_width=True,hide_index=True)
    export_buttons(media_data, "medias", "Presse et médias CRA/ISRA")

# -----------------------------
# AUDIOVISUAL
# -----------------------------
elif page == "🎥 Audiovisuel":
    st.header("🎥 Productions audiovisuelles")
    with st.form("av_form"):
        c1,c2,c3=st.columns(3)
        titre=c1.text_input("Titre *"); typ=c2.selectbox("Type",["Film","Vidéo","Interview","Reportage","Documentaire","Autre"]); dp=c3.date_input("Date",date.today())
        c1,c2,c3=st.columns(3)
        lieu=c1.text_input("Lieu"); theme=c2.text_input("Thème"); duree=c3.text_input("Durée")
        interviewes=st.text_area("Personnes interviewées")
        responsable=st.text_input("Responsable"); statut=st.selectbox("Statut",["Prévu","En préparation","En production","Terminé","Diffusé"])
        lien=st.text_input("Lien de diffusion"); fichier=st.file_uploader("Fichier original",type=["mp4","mov","avi","mkv"])
        observations=st.text_area("Observations")
        ok=st.form_submit_button("💾 Enregistrer",type="primary")
    if ok:
        path=save_uploaded(fichier,"audiovisuel")
        q("""INSERT INTO audiovisual(titre,type_production,date_production,lieu,theme,interviewes,duree,responsable,statut,lien,fichier_original,observations)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(titre,typ,str(dp),lieu,theme,interviewes,duree,responsable,statut,lien,path,observations))
        st.success("Production enregistrée."); st.rerun()
    av_data = df("SELECT * FROM audiovisual ORDER BY date_production DESC")
    st.dataframe(av_data,use_container_width=True,hide_index=True)
    export_buttons(av_data, "audiovisuel", "Productions audiovisuelles CRA/ISRA")

# -----------------------------
# RESEARCHERS
# -----------------------------
elif page == "🔬 Valorisation chercheurs":
    st.header("🔬 Valorisation des chercheurs")
    people=options_people()
    with st.form("researcher_form"):
        person_label=st.selectbox("Chercheur",["—"]+list(people))
        c1,c2,c3=st.columns(3)
        da=c1.date_input("Date",date.today()); domaine=c2.text_input("Domaine"); theme=c3.text_input("Thématique")
        projet=st.text_input("Projet")
        type_val=c1.selectbox("Type de valorisation",["Article","Interview","Portrait","Vidéo","Publication","Communication scientifique","Autre"])
        support=c2.text_input("Support")
        lien=c3.text_input("Lien")
        observations=st.text_area("Observations")
        ok=st.form_submit_button("💾 Enregistrer",type="primary")
    if ok:
        pid=people.get(person_label)
        q("""INSERT INTO researcher_valorization(person_id,date_action,domaine,thematique,projet,type_valorisation,support,lien,observations)
             VALUES(?,?,?,?,?,?,?,?,?)""",(pid,str(da),domaine,theme,projet,type_val,support,lien,observations))
        st.success("Valorisation enregistrée."); st.rerun()
    research_data = df("""SELECT rv.id, p.nom||' '||COALESCE(p.prenom,'') AS chercheur, rv.date_action,
                       rv.domaine,rv.thematique,rv.projet,rv.type_valorisation,rv.support,rv.lien
                       FROM researcher_valorization rv LEFT JOIN people p ON p.id=rv.person_id
                       ORDER BY rv.date_action DESC""")
    st.dataframe(research_data,use_container_width=True,hide_index=True)
    export_buttons(research_data, "valorisation_chercheurs", "Valorisation des chercheurs CRA/ISRA")

# -----------------------------
# DOCUMENTATION
# -----------------------------
elif page == "📄 Documentation / IST":
    st.header("📄 Documentation / IST")
    with st.form("doc_form"):
        c1,c2,c3=st.columns(3)
        titre=c1.text_input("Titre *"); auteurs=c2.text_input("Auteur(s)"); annee=c3.number_input("Année",1900,2100,value=date.today().year)
        c1,c2,c3=st.columns(3)
        typ=c1.selectbox("Type",["Article scientifique","Mémoire","Thèse","Rapport","Communication","Publication","Document technique","Guide","Brochure","Plaquette","Revue","Bulletin","Autre"])
        theme=c2.text_input("Thématique"); mots=c3.text_input("Mots-clés")
        chercheur=st.text_input("Chercheur associé"); projet=st.text_input("Projet")
        resume=st.text_area("Résumé")
        c1,c2,c3=st.columns(3)
        langue=c1.text_input("Langue",value="Français"); pages=c2.number_input("Pages",0,step=1); reference=c3.text_input("Référence")
        lien=st.text_input("Lien")
        fichier=st.file_uploader("Fichier numérique",type=["pdf","doc","docx","txt","xlsx"])
        statut=st.selectbox("Statut",["Disponible","À vérifier","Archivé"])
        observations=st.text_area("Observations")
        ok=st.form_submit_button("💾 Ajouter le document",type="primary")
    if ok:
        path=save_uploaded(fichier,"documents")
        q("""INSERT INTO documents(titre,auteurs,annee,type_document,thematique,mots_cles,chercheur_associe,projet,resume,langue,pages,reference,fichier,lien,statut,observations)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(titre,auteurs,annee,typ,theme,mots,chercheur,projet,resume,langue,pages,reference,path,lien,statut,observations))
        st.success("Document enregistré."); st.rerun()
    doc_data = df("SELECT id,titre,auteurs,annee,type_document,thematique,reference,statut FROM documents ORDER BY annee DESC,id DESC")
    st.dataframe(doc_data,use_container_width=True,hide_index=True)
    export_buttons(doc_data, "documentation_ist", "Documentation / IST CRA/ISRA")

# -----------------------------
# LIBRARY CATALOG
# -----------------------------
elif page == "📚 Bibliothèque":
    st.header("📚 Catalogue et gestion des documents")
    with st.form("lib_form"):
        c1,c2,c3=st.columns(3)
        inventaire=c1.text_input("N° inventaire"); cote=c2.text_input("Cote"); isbn=c3.text_input("ISBN")
        titre=c1.text_input("Titre *"); sous_titre=c2.text_input("Sous-titre"); auteurs=c3.text_input("Auteur(s)")
        c1,c2,c3=st.columns(3)
        editeur=c1.text_input("Éditeur"); annee=c2.number_input("Année",0,2100,value=date.today().year); typ=c3.selectbox("Type",["Livre","Article","Rapport","Mémoire","Thèse","Document technique","Publication scientifique","Brochure","Plaquette","Revue","Bulletin","Actes de conférence","Document audiovisuel","Autre"])
        c1,c2,c3=st.columns(3)
        domaine=c1.text_input("Domaine"); theme=c2.text_input("Thématique"); mots=c3.text_input("Mots-clés")
        c1,c2,c3=st.columns(3)
        exemplaires=c1.number_input("Exemplaires",1,10000,1); disponibles=c2.number_input("Disponibles",0,10000,1); localisation=c3.text_input("Localisation")
        etat=c1.selectbox("État",["Bon","Moyen","À restaurer","Endommagé"]); format_doc=c2.selectbox("Format",["Papier","Numérique","Papier + numérique"]); lien=c3.text_input("Lien")
        resume=st.text_area("Résumé")
        fichier=st.file_uploader("Fichier numérique",type=["pdf","doc","docx"],key="libfile")
        observations=st.text_area("Observations")
        ok=st.form_submit_button("💾 Ajouter au catalogue",type="primary")
    if ok:
        try:
            path=save_uploaded(fichier,"bibliotheque")
            q("""INSERT INTO library_items(inventaire,cote,isbn,titre,sous_titre,auteurs,editeur,annee,type_document,domaine,thematique,mots_cles,exemplaires,disponibles,localisation,etat,format_document,resume,fichier,lien,observations)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(inventaire,cote,isbn,titre,sous_titre,auteurs,editeur,annee,typ,domaine,theme,mots,exemplaires,disponibles,localisation,etat,format_doc,resume,path,lien,observations))
            st.success("Document ajouté à la bibliothèque."); st.rerun()
        except Exception as exc:
            if 'duplicate key' in str(exc).lower() or 'unique constraint' in str(exc).lower():
                st.error("Le numéro d'inventaire existe déjà.")
            else:
                st.error(f"Erreur PostgreSQL / Storage : {exc}")
    st.subheader("Catalogue")
    library_catalog = df("SELECT id,inventaire,cote,titre,auteurs,annee,type_document,exemplaires,disponibles,localisation,etat FROM library_items ORDER BY titre")
    st.dataframe(library_catalog,use_container_width=True,hide_index=True)
    export_buttons(library_catalog, "catalogue_bibliotheque", "Catalogue de la bibliothèque CRA/ISRA")

# -----------------------------
# VISITORS
# -----------------------------
elif page == "👤 Visiteurs":
    st.header("👤 Registre des visiteurs")
    people=options_people()
    with st.form("visit_form"):
        person_label=st.selectbox("Personne",["—"]+list(people))
        c1,c2,c3=st.columns(3)
        dv=c1.date_input("Date de visite",date.today()); ha=c2.text_input("Heure d'arrivée"); hd=c3.text_input("Heure de départ")
        motif=st.text_input("Motif de la visite")
        docs=st.text_area("Documents consultés")
        emprunts=st.text_area("Documents empruntés")
        observations=st.text_area("Observations")
        ok=st.form_submit_button("💾 Enregistrer la visite",type="primary")
    if ok:
        q("""INSERT INTO library_visits(person_id,date_visite,heure_arrivee,heure_depart,motif,documents_consultes,documents_empruntes,observations)
             VALUES(?,?,?,?,?,?,?,?)""",(people.get(person_label),str(dv),ha,hd,motif,docs,emprunts,observations))
        st.success("Visite enregistrée."); st.rerun()
    visits=df("""SELECT v.id,v.date_visite,p.nom||' '||COALESCE(p.prenom,'') AS visiteur,
                 p.structure,p.categorie,v.heure_arrivee,v.heure_depart,v.motif
                 FROM library_visits v LEFT JOIN people p ON p.id=v.person_id
                 ORDER BY v.date_visite DESC,v.id DESC""")
    st.dataframe(visits,use_container_width=True,hide_index=True)
    export_buttons(visits, "visiteurs", "Registre des visiteurs CRA/ISRA")
    st.metric("Nombre total de visiteurs enregistrés",len(visits))

# -----------------------------
# CONSULTATIONS
# -----------------------------
elif page == "📖 Consultations":
    st.header("📖 Consultations des documents")
    people=options_people(); items=options_items()
    with st.form("consult_form"):
        person=st.selectbox("Personne",["—"]+list(people))
        item=st.selectbox("Document",["—"]+list(items))
        c1,c2=st.columns(2)
        dc=c1.date_input("Date",date.today()); heure=c2.text_input("Heure")
        typ=c1.selectbox("Type",["Consultation sur place","Consultation numérique","Autre"])
        observations=c2.text_input("Observations")
        ok=st.form_submit_button("💾 Enregistrer",type="primary")
    if ok:
        q("""INSERT INTO consultations(person_id,item_id,date_consultation,heure,type_consultation,observations)
             VALUES(?,?,?,?,?,?)""",(people.get(person),items.get(item),str(dc),heure,typ,observations))
        st.success("Consultation enregistrée."); st.rerun()
    consultations_data = df("""SELECT c.id,c.date_consultation,
                 p.nom||' '||COALESCE(p.prenom,'') AS personne,i.titre,
                 c.type_consultation,c.observations
                 FROM consultations c LEFT JOIN people p ON p.id=c.person_id
                 LEFT JOIN library_items i ON i.id=c.item_id
                 ORDER BY c.date_consultation DESC""")
    st.dataframe(consultations_data,use_container_width=True,hide_index=True)
    export_buttons(consultations_data, "consultations", "Consultations des documents CRA/ISRA")

# -----------------------------
# LOANS
# -----------------------------
elif page == "📕 Prêts / Retours":
    st.header("📕 Prêts, retours et échéances")
    people=options_people(); items=options_items()
    with st.form("loan_form"):
        person=st.selectbox("Emprunteur",["—"]+list(people))
        item=st.selectbox("Document",["—"]+list(items))
        c1,c2=st.columns(2)
        de=c1.date_input("Date d'emprunt",date.today()); dr=c2.date_input("Date prévue de retour",date.today()+timedelta(days=14))
        observations=st.text_area("Observations")
        ok=st.form_submit_button("📕 Enregistrer le prêt",type="primary")
    if ok:
        iid=items.get(item)
        dispo=scalar("SELECT disponibles FROM library_items WHERE id=?",(iid,)) if iid else 0
        if not people.get(person) or not iid: st.error("Sélectionnez l'emprunteur et le document.")
        elif dispo <= 0: st.error("Aucun exemplaire disponible.")
        else:
            q("""INSERT INTO loans(person_id,item_id,date_emprunt,date_retour_prevue,statut,observations)
                 VALUES(?,?,?,?,?,?)""",(people.get(person),iid,str(de),str(dr),"En cours",observations))
            q("UPDATE library_items SET disponibles=disponibles-1 WHERE id=?",(iid,))
            st.success("Prêt enregistré."); st.rerun()

    loans=df("""SELECT l.id,p.nom||' '||COALESCE(p.prenom,'') AS emprunteur,
                i.titre,i.inventaire,l.date_emprunt,l.date_retour_prevue,
                l.date_retour_reelle,l.statut,l.observations
                FROM loans l LEFT JOIN people p ON p.id=l.person_id
                LEFT JOIN library_items i ON i.id=l.item_id
                ORDER BY l.date_retour_prevue""")
    if not loans.empty:
        # Update overdue statuses
        today=date.today().isoformat()
        q("""UPDATE loans SET statut='En retard'
             WHERE statut='En cours' AND date_retour_prevue < ?""",(today,))
        loans=df("""SELECT l.id,p.nom||' '||COALESCE(p.prenom,'') AS emprunteur,
                i.titre,i.inventaire,l.date_emprunt,l.date_retour_prevue,
                l.date_retour_reelle,l.statut,l.observations
                FROM loans l LEFT JOIN people p ON p.id=l.person_id
                LEFT JOIN library_items i ON i.id=l.item_id
                ORDER BY l.date_retour_prevue""")
    st.subheader("Registre des prêts")
    st.dataframe(loans,use_container_width=True,hide_index=True)
    export_buttons(loans, "prets_retours", "Registre des prêts et retours CRA/ISRA")

    st.subheader("↩️ Enregistrer un retour")
    active=df("""SELECT l.id,p.nom||' '||COALESCE(p.prenom,'') AS emprunteur,i.titre,l.date_retour_prevue,l.statut
                  FROM loans l LEFT JOIN people p ON p.id=l.person_id LEFT JOIN library_items i ON i.id=l.item_id
                  WHERE l.statut IN ('En cours','En retard') ORDER BY l.date_retour_prevue""")
    if not active.empty:
        labels={f"#{r.id} — {r.emprunteur} — {r.titre} — retour prévu {r.date_retour_prevue}":r.id for r in active.itertuples()}
        chosen=st.selectbox("Prêt à retourner",list(labels))
        retour=st.date_input("Date réelle de retour",date.today(),key="return_date")
        if st.button("↩️ Valider le retour",type="primary"):
            lid=labels[chosen]
            iid=scalar("SELECT item_id FROM loans WHERE id=?",(lid,))
            q("UPDATE loans SET date_retour_reelle=?,statut='Retourné' WHERE id=?",(str(retour),lid))
            q("UPDATE library_items SET disponibles=disponibles+1 WHERE id=?",(iid,))
            st.success("Retour enregistré."); st.rerun()
    else:
        st.info("Aucun prêt en cours.")

# -----------------------------
# PROFILE
# -----------------------------
elif page == "👤 Profil":
    st.markdown("""
    <div class="hero" style="min-height:210px;background:
         radial-gradient(circle at 85% 20%,rgba(45,138,74,.55),transparent 25%),
         linear-gradient(120deg,#061F3A,#0B74B8 65%,#2D8A4A);">
      <div class="hero-badge">PROFIL • CRA / ISRA • SAINT-LOUIS</div>
      <h1 style="font-size:2.35rem;">Ndeye Fota Gueye</h1>
      <p style="font-size:1.05rem;">Chargée de la communication et documentation de CRA/ISRA Saint-Louis</p>
    </div>
    """, unsafe_allow_html=True)
    c1, c2 = st.columns([1,2])
    with c1:
        st.markdown('<div class="metric"><div class="label">Fonction</div><div class="value" style="font-size:1.1rem;">Communication & Documentation</div></div>', unsafe_allow_html=True)
    with c2:
        st.markdown('<div class="card"><h3>Mission dans l’application</h3><p>Suivi des activités, communication, médias, audiovisuel, valorisation des chercheurs, documentation/IST, bibliothèque, visiteurs, consultations, prêts/retours et production des rapports.</p></div>', unsafe_allow_html=True)
    st.info("Les exports PDF utilisent désormais un format A4 portrait, avec couverture graphique, en-têtes/pieds de page et toutes les données des registres exportés.")

# -----------------------------
# REPORTS
# -----------------------------
elif page == "📊 Rapports & exports":
    st.header("📊 Rapports, statistiques et exports")
    st.markdown("""
    <div class="card" style="background:linear-gradient(135deg,#083B66,#0B74B8);color:white;
         border:none;animation:fadeInUp .55s ease-out;">
      <div style="font-size:.78rem;letter-spacing:.12em;opacity:.8;">PROFIL RESPONSABLE</div>
      <div style="font-size:1.55rem;font-weight:800;margin-top:5px;">Ndeye Fota Gueye</div>
      <div style="font-size:.95rem;margin-top:4px;opacity:.94;">
        Chargée de la communication et documentation de CRA/ISRA Saint-Louis
      </div>
    </div>
    """, unsafe_allow_html=True)
    c1,c2=st.columns(2)
    start=c1.date_input("Du",date(date.today().year,1,1))
    end=c2.date_input("Au",date.today())
    st.subheader("Indicateurs sur la période")
    vals = [
        ("Activités", scalar("SELECT COUNT(*) FROM activities WHERE date_activite BETWEEN ? AND ?",(str(start),str(end)))),
        ("Visiteurs", scalar("SELECT COUNT(*) FROM library_visits WHERE date_visite BETWEEN ? AND ?",(str(start),str(end)))),
        ("Consultations", scalar("SELECT COUNT(*) FROM consultations WHERE date_consultation BETWEEN ? AND ?",(str(start),str(end)))),
        ("Prêts", scalar("SELECT COUNT(*) FROM loans WHERE date_emprunt BETWEEN ? AND ?",(str(start),str(end)))),
        ("Communications", scalar("SELECT COUNT(*) FROM communications WHERE date_publication BETWEEN ? AND ?",(str(start),str(end)))),
        ("Médias", scalar("SELECT COUNT(*) FROM media WHERE date_intervention BETWEEN ? AND ?",(str(start),str(end)))),
        ("Productions audiovisuelles", scalar("SELECT COUNT(*) FROM audiovisual WHERE date_production BETWEEN ? AND ?",(str(start),str(end)))),
    ]
    cols=st.columns(4)
    for i,(label,val) in enumerate(vals):
        with cols[i%4]:
            st.metric(label,val)

    activities=df("SELECT * FROM activities WHERE date_activite BETWEEN ? AND ? ORDER BY date_activite",(str(start),str(end)))
    visits=df("SELECT * FROM library_visits WHERE date_visite BETWEEN ? AND ? ORDER BY date_visite",(str(start),str(end)))
    loans=df("SELECT * FROM loans WHERE date_emprunt BETWEEN ? AND ? ORDER BY date_emprunt",(str(start),str(end)))
    communications=df("SELECT * FROM communications WHERE date_publication BETWEEN ? AND ? ORDER BY date_publication",(str(start),str(end)))
    media_df=df("SELECT * FROM media WHERE date_intervention BETWEEN ? AND ? ORDER BY date_intervention",(str(start),str(end)))
    av=df("SELECT * FROM audiovisual WHERE date_production BETWEEN ? AND ? ORDER BY date_production",(str(start),str(end)))
    documents=df("SELECT * FROM documents ORDER BY annee DESC,id DESC")
    library=df("SELECT * FROM library_items ORDER BY titre")
    report_data = {
        "Activités": activities, "Visiteurs": visits, "Prêts": loans,
        "Communication": communications, "Médias": media_df,
        "Audiovisuel": av, "Documents IST": documents, "Bibliothèque": library
    }

    # Rapport PDF global A4 : toutes les rubriques et toutes leurs colonnes.
    full_pdf = complete_report_pdf(report_data, start, end)
    if full_pdf is not None:
        st.download_button(
            "✨ Télécharger le RAPPORT COMPLET A4 — design premium",
            full_pdf,
            "rapport_cra_isra_complet_A4.pdf",
            "application/pdf",
            type="primary",
            use_container_width=True
        )

    # Rapport Excel complet multi-feuilles
    st.download_button(
        "⬇️ Télécharger le rapport Excel complet",
        excel_bytes(report_data),
        "rapport_cra_isra_complet.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary"
    )
    # CSV et PDF pour la synthèse de période
    summary = pd.DataFrame({
        "Indicateur": [x[0] for x in vals],
        "Valeur": [x[1] for x in vals],
        "Période": [f"{start} → {end}"] * len(vals)
    })
    export_buttons(summary, "synthese_cra_isra", "Synthèse du rapport CRA/ISRA")
    st.subheader("Activités de la période")
    st.dataframe(activities,use_container_width=True,hide_index=True)
    st.subheader("Bibliothèque — fréquentation et prêts")
    c1,c2=st.columns(2)
    with c1: st.dataframe(visits,use_container_width=True,hide_index=True)
    with c2: st.dataframe(loans,use_container_width=True,hide_index=True)

st.caption("ISRA • CRA — Registre interne de communication, documentation, IST et bibliothèque | Formats disponibles : PDF • Excel • CSV")
