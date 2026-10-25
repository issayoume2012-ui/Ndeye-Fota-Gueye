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
.section-banner { background:linear-gradient(135deg,#061f3a,#0b74b8 58%,#2d8a4a); color:#fff; border-radius:28px; padding:24px 30px; margin:18px 0 26px; box-shadow:0 18px 55px rgba(6,31,58,.18); }
.section-banner span { display:block; font-size:.72rem; letter-spacing:.18em; opacity:.72; margin-bottom:6px; }
.section-banner strong { font-size:1.45rem; }
.stTabs [data-baseweb="tab-list"] { gap:10px; padding:8px; background:rgba(255,255,255,.78); border-radius:20px; box-shadow:0 10px 30px rgba(20,60,100,.08); }
.stTabs [data-baseweb="tab"] { min-height:48px; padding:10px 18px; border-radius:14px; font-weight:800; }
[data-testid="stMetric"] { border-radius:20px; padding:18px; box-shadow:0 10px 30px rgba(20,60,100,.08); background:rgba(255,255,255,.92); }
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
            observations TEXT,
            visitor_name TEXT,
            visitor_structure TEXT,
            visitor_categorie TEXT,
            visitor_contact TEXT,
            visitor_email TEXT,
            visitor_lien TEXT
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
        CREATE TABLE IF NOT EXISTS researcher_profiles (
            id BIGSERIAL PRIMARY KEY,
            nom TEXT NOT NULL,
            prenom TEXT,
            grade_fonction TEXT,
            unite_laboratoire TEXT,
            domaine_expertise TEXT,
            mots_cles TEXT,
            biographie TEXT,
            profil_complet TEXT,
            email TEXT,
            telephone TEXT,
            ORCID TEXT,
            photo TEXT,
            cv_fichier TEXT,
            lien_profil TEXT,
            observations TEXT,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS researcher_valorization_researchers (
            id BIGSERIAL PRIMARY KEY,
            valorization_id BIGINT NOT NULL REFERENCES researcher_valorization(id) ON DELETE CASCADE,
            researcher_id BIGINT NOT NULL REFERENCES researcher_profiles(id) ON DELETE CASCADE,
            UNIQUE(valorization_id,researcher_id)
        );
        CREATE TABLE IF NOT EXISTS evidence (
            id BIGSERIAL PRIMARY KEY,
            activity_id BIGINT REFERENCES activities(id) ON DELETE CASCADE,
            nom_fichier TEXT NOT NULL,
            chemin TEXT,
            type_fichier TEXT,
            date_ajout TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS communication_formats (
            id BIGSERIAL PRIMARY KEY,
            categorie TEXT NOT NULL,
            nom_format TEXT NOT NULL,
            description TEXT,
            usage_recommande TEXT,
            norme TEXT,
            lien_reference TEXT,
            observations TEXT,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS scientific_results (
            id BIGSERIAL PRIMARY KEY,
            type_resultat TEXT NOT NULL,
            titre TEXT NOT NULL,
            auteurs TEXT,
            annee INTEGER,
            domaine TEXT,
            projet TEXT,
            resume TEXT,
            mots_cles TEXT,
            public_cible TEXT,
            reference TEXT,
            fichier TEXT,
            lien TEXT,
            statut TEXT DEFAULT 'Disponible',
            observations TEXT,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS scientific_publications (
            id BIGSERIAL PRIMARY KEY,
            type_publication TEXT NOT NULL,
            titre TEXT NOT NULL,
            auteurs TEXT,
            annee INTEGER,
            domaine TEXT,
            projet TEXT,
            resume TEXT,
            mots_cles TEXT,
            public_cible TEXT,
            reference TEXT,
            fichier TEXT,
            lien TEXT,
            statut TEXT DEFAULT 'Disponible',
            observations TEXT,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        ALTER TABLE activities ADD COLUMN IF NOT EXISTS date_fin DATE;
        UPDATE activities SET date_fin = date_activite WHERE date_fin IS NULL AND date_activite IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date_activite);
        CREATE INDEX IF NOT EXISTS idx_documents_year ON documents(annee);
        CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(statut);
        CREATE INDEX IF NOT EXISTS idx_visits_date ON library_visits(date_visite);
        ALTER TABLE library_visits ADD COLUMN IF NOT EXISTS visitor_name TEXT;
        ALTER TABLE library_visits ADD COLUMN IF NOT EXISTS visitor_structure TEXT;
        ALTER TABLE library_visits ADD COLUMN IF NOT EXISTS visitor_categorie TEXT;
        ALTER TABLE library_visits ADD COLUMN IF NOT EXISTS visitor_contact TEXT;
        ALTER TABLE library_visits ADD COLUMN IF NOT EXISTS visitor_email TEXT;
        ALTER TABLE library_visits ADD COLUMN IF NOT EXISTS visitor_lien TEXT;
        CREATE INDEX IF NOT EXISTS idx_storage_audiovisual ON audiovisual(fichier_original);
        CREATE INDEX IF NOT EXISTS idx_storage_documents ON documents(fichier);
        CREATE INDEX IF NOT EXISTS idx_storage_library ON library_items(fichier);
        CREATE INDEX IF NOT EXISTS idx_scientific_results_type ON scientific_results(type_resultat);
        CREATE INDEX IF NOT EXISTS idx_researcher_profiles_name ON researcher_profiles(nom,prenom);
        CREATE INDEX IF NOT EXISTS idx_researcher_valorization_link ON researcher_valorization_researchers(valorization_id,researcher_id);
        CREATE INDEX IF NOT EXISTS idx_scientific_publications_type ON scientific_publications(type_publication);
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
        # IMPORTANT : ne pas passer un tuple de paramètres vide à psycopg2.
        # Avec des requêtes contenant des motifs LIKE tels que '%rapport%',
        # psycopg2 peut interpréter les signes % comme des marqueurs de format
        # et lever IndexError: tuple index out of range. Sans paramètres,
        # exécuter la requête sans second argument préserve les % littéraux.
        prepared_sql = _sql(sql)
        if params is None or params == () or params == []:
            cur.execute(prepared_sql)
        else:
            cur.execute(prepared_sql, params)
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

def _excel_safe_dataframe(data):
    """Prépare un DataFrame pour Excel sans modifier les données en base.

    Supabase/PostgreSQL peut renvoyer des timestamps avec fuseau horaire,
    ainsi que des listes/dictionnaires. openpyxl refuse notamment les
    datetime timezone-aware. On convertit uniquement la copie destinée à
    l'export Excel et on conserve les données originales dans l'application.
    """
    if data is None:
        return pd.DataFrame()

    out = data.copy()

    for col in out.columns:
        series = out[col]

        # Excel/openpyxl n'accepte pas les datetime avec timezone.
        if pd.api.types.is_datetime64_any_dtype(series):
            try:
                if getattr(series.dt, "tz", None) is not None:
                    out[col] = series.dt.tz_localize(None)
            except (TypeError, AttributeError):
                out[col] = series.map(
                    lambda v: v.replace(tzinfo=None) if hasattr(v, "tzinfo") and v.tzinfo else v
                )
            continue

        # Les colonnes object peuvent contenir des dates timezone-aware
        # renvoyées directement par PostgreSQL/Supabase.
        if series.dtype == "object":
            def safe_value(value):
                if value is None:
                    return None
                if isinstance(value, (list, tuple, set, dict)):
                    return json.dumps(value, ensure_ascii=False, default=str)
                if isinstance(value, (datetime, pd.Timestamp)):
                    if getattr(value, "tzinfo", None) is not None:
                        try:
                            return value.replace(tzinfo=None)
                        except Exception:
                            return str(value)
                    return value
                try:
                    missing = pd.isna(value)
                    if isinstance(missing, bool) and missing:
                        return None
                except Exception:
                    pass
                if hasattr(value, "tzinfo") and value.tzinfo is not None:
                    try:
                        return value.replace(tzinfo=None)
                    except Exception:
                        return str(value)
                return value
            out[col] = series.map(safe_value)

    return out


def excel_bytes(dataframes):
    """Génère un fichier Excel robuste pour les données CRA/ISRA."""
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        for name, data in dataframes.items():
            safe_data = _excel_safe_dataframe(data)
            safe_data.to_excel(writer, sheet_name=str(name)[:31], index=False)
    return bio.getvalue()


def csv_bytes(data):
    return data.to_csv(index=False).encode("utf-8-sig")


def _pdf_header_footer(canvas, doc):
    """Habillage A4 premium cohérent avec la nouvelle architecture de l'application."""
    canvas.saveState()
    w, h = A4

    navy = colors.HexColor("#062A49")
    blue = colors.HexColor("#0B74B8")
    green = colors.HexColor("#2D8A4A")
    pale = colors.HexColor("#EEF6FB")
    grey = colors.HexColor("#64748B")
    line = colors.HexColor("#D7E5EF")

    # Bandeau supérieur
    canvas.setFillColor(navy)
    canvas.rect(0, h - 64, w, 64, fill=1, stroke=0)
    canvas.setFillColor(blue)
    canvas.rect(0, h - 68, w, 4, fill=1, stroke=0)
    canvas.setFillColor(green)
    canvas.rect(0, h - 71, w, 3, fill=1, stroke=0)

    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(34, h - 31, "ISRA • CRA")
    canvas.setFont("Helvetica", 7.2)
    canvas.drawRightString(
        w - 34,
        h - 31,
        "IST • COMMUNICATION • DOCUMENTATION • PUBLICATIONS & DOCUMENTS SCIENTIFIQUES"
    )

    # Ligne de pied
    canvas.setStrokeColor(line)
    canvas.line(34, 31, w - 34, 31)
    canvas.setFillColor(grey)
    canvas.setFont("Helvetica", 7)
    canvas.drawString(34, 19, "Registre interne CRA/ISRA • Ndeye Fota Gueye")
    canvas.drawRightString(w - 34, 19, f"Page {doc.page}")

    canvas.restoreState()


def _safe_para(value, style):
    """Transforme toute valeur en Paragraph ReportLab sans casser les caractères spéciaux."""
    if value is None:
        value = ""
    value = str(value)
    value = (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )
    return Paragraph(value, style)


def _pdf_table(data, body_style, head_style, usable_width=523, max_rows=None):
    """
    Tableau PDF robuste :
    - conserve les données ;
    - répartit intelligemment la largeur ;
    - évite qu'une table très large sorte de la page ;
    - répète l'en-tête sur les pages suivantes.
    """
    if data is None:
        data = pd.DataFrame()

    d = data.copy()
    if d.empty:
        return None

    d = d.fillna("")
    headers = [str(c) for c in d.columns]

    table_data = [[_safe_para(c, head_style) for c in headers]]

    limited = d if max_rows is None else d.head(max_rows)
    for row in limited.astype(str).values.tolist():
        table_data.append([_safe_para(v, body_style) for v in row])

    # Poids des colonnes.
    # Les tables très larges restent lisibles grâce à une largeur minimale
    # et à un poids proportionnel au contenu.
    weights = []
    for col in limited.columns:
        values = [str(col)] + [str(x) for x in limited[col].head(80).tolist()]
        max_len = max([len(x) for x in values] or [1])
        weights.append(min(max(max_len * 2.4, 28), 105))

    total = sum(weights) or 1
    widths = [usable_width * weight / total for weight in weights]

    tbl = Table(
        table_data,
        colWidths=widths,
        repeatRows=1,
        hAlign="LEFT",
        splitByRow=1,
        splitInRow=1,
    )
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#062A49")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 6.6),
        ("FONTSIZE", (0, 1), (-1, -1), 5.9),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#C7D8E5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [
            colors.white,
            colors.HexColor("#F6FAFD")
        ]),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return tbl


def _df_table(data, body_style, head_style):
    """Compatibilité avec les exports PDF individuels existants."""
    return _pdf_table(data, body_style, head_style)


def _pdf_section_header(number, title, subtitle, style):
    """Bloc visuel de section pour le rapport premium."""
    content = [
        Paragraph(
            f'<font color="#2D8A4A"><b>{number}</b></font> &nbsp; '
            f'<b>{title}</b>',
            style
        )
    ]
    if subtitle:
        content.append(
            Paragraph(
                str(subtitle).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
                ParagraphStyle(
                    f"SectionSub_{number}",
                    parent=style,
                    fontSize=7.5,
                    leading=10,
                    textColor=colors.HexColor("#64748B"),
                    spaceBefore=1,
                    spaceAfter=6,
                )
            )
        )
    return content


def _pdf_kpi_table(kpis, body_style):
    """KPI compact et premium pour la synthèse du rapport."""
    if not kpis:
        return None

    cells = []
    for label, value in kpis:
        cells.append(
            Paragraph(
                f'<font size="7" color="#64748B">{str(label)}</font><br/>'
                f'<font size="17" color="#083B66"><b>{str(value)}</b></font>',
                body_style
            )
        )

    cols = min(4, len(cells))
    rows = []
    for i in range(0, len(cells), cols):
        row = cells[i:i + cols]
        while len(row) < cols:
            row.append(Paragraph("", body_style))
        rows.append(row)

    widths = [523 / cols] * cols
    tbl = Table(rows, colWidths=widths, hAlign="LEFT")
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F4F9FC")),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#D7E5EF")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D7E5EF")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    return tbl


def _is_activity_dataset(frame, title=""):
    """Détecte l'export des activités sans dépendre de tous les libellés exacts."""
    if frame is None or frame.empty:
        return False
    cols = {str(c).strip().lower().replace("’", "'") for c in frame.columns}
    title_text = str(title).lower()
    has_activity_title = any(x in title_text for x in ("activité", "activite", "activités", "activites"))
    has_activity_fields = (
        any(c in cols for c in ("titre", "intitulé", "intitule"))
        and any(c in cols for c in ("heure_debut", "heure début", "heure de début"))
        and any(c in cols for c in ("type_activite", "type d’activité", "type d'activite", "type"))
    )
    return has_activity_title or has_activity_fields


def _activity_color(index):
    """Retourne une couleur pastel distincte pour chaque activité du PDF."""
    palette = [
        ("#EAF4FF", "#91BDEB", "#173B63"),  # bleu clair
        ("#EAF8EF", "#91CBA3", "#205638"),  # vert clair
        ("#FFF3E6", "#E8B982", "#75451D"),  # pêche clair
        ("#F5EDFF", "#BDA3E8", "#51357A"),  # violet clair
        ("#FFF0F3", "#E5A2B0", "#7A3445"),  # rose clair
        ("#FFF9DF", "#E2CE75", "#665515"),  # jaune clair
        ("#E6FAF8", "#8CCFC7", "#205C58"),  # turquoise clair
        ("#F0F2F5", "#B7C0CC", "#374151"),  # gris clair
        ("#FBEFE5", "#DDB49A", "#70452D"),  # beige clair
        ("#EEF5E5", "#B5CD8E", "#435B26"),  # vert olive clair
    ]
    return palette[(index - 1) % len(palette)]


def _append_activity_pdf_cards(story, frame, body_style, section_style):
    """Fiches colorées par activité ; chaque champ reste regroupé dans un seul bloc."""
    page_width = 523
    label_width, value_width = 112, 411
    metadata_keys = {
        "date_activite": "Date activité", "date activité": "Date activité", "date": "Date activité",
        "heure_debut": "Heure début", "heure début": "Heure début", "heure de début": "Heure début",
        "heure_fin": "Heure fin", "heure fin": "Heure fin", "heure de fin": "Heure fin",
        "date_fin": "Date fin", "date fin": "Date fin",
        "duree_jours": "Durée (jours)", "durée (jours)": "Durée (jours)", "duree jours": "Durée (jours)",
        "statut": "Statut", "created_at": "Créé le", "created at": "Créé le",
    }
    metadata_order = ["Date activité", "Date fin", "Durée (jours)", "Heure début", "Heure fin", "Statut", "Créé le"]

    def clean(value):
        if value is None:
            return ""
        result = str(value).strip()
        return "" if result.lower() in ("nan", "none", "nat") else result

    def nice_date(value):
        text = clean(value)
        # Présente les dates ISO dans un format lisible, sans modifier la donnée source.
        try:
            if len(text) >= 10 and text[4] == "-" and text[7] == "-":
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if "T" in text or " " in text:
                    return parsed.strftime("%d/%m/%Y %H:%M")
                return parsed.strftime("%d/%m/%Y")
        except (ValueError, TypeError):
            pass
        return text

    for index, (_, row) in enumerate(frame.iterrows(), start=1):
        fill_hex, border_hex, text_hex = _activity_color(index)
        fill, border = colors.HexColor(fill_hex), colors.HexColor(border_hex)
        label_style = ParagraphStyle(
            f"ActivityFieldLabel{index}", parent=body_style, fontName="Helvetica-Bold",
            fontSize=7.2, leading=9, textColor=colors.HexColor(text_hex),
        )
        activity_style = ParagraphStyle(
            f"ActivityCardText{index}", parent=body_style, fontSize=7.2, leading=9,
            textColor=colors.HexColor(text_hex), wordWrap="CJK",
        )
        normalized = {str(key).strip().lower().replace("’", "'"): key for key in row.index}
        raw_title = ""
        for candidate in ("titre", "intitulé", "intitule"):
            if candidate in normalized:
                raw_title = clean(row[normalized[candidate]])
                break
        heading = f"Activité {index}" + (f" — {raw_title}" if raw_title else "")
        heading_style = ParagraphStyle(
            f"ActivityHeading{index}", parent=section_style, textColor=colors.HexColor(text_hex),
            fontSize=10, leading=12, spaceBefore=0, spaceAfter=0,
        )
        heading_table = Table([[Paragraph(_safe_para_text(heading), heading_style)]], colWidths=[page_width], hAlign="LEFT")
        heading_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), fill), ("BOX", (0, 0), (-1, -1), 0.7, border),
            ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(Spacer(1, 5))
        story.append(heading_table)

        # Les métadonnées utiles sont regroupées en haut de la fiche, sur une ligne
        # compacte à plusieurs cellules, plutôt que dispersées dans les descriptions.
        meta_cells = []
        for wanted in metadata_order:
            actual_key = next((key for key in row.index if metadata_keys.get(str(key).strip().lower().replace("’", "'")) == wanted), None)
            if actual_key is not None:
                val = clean(row[actual_key])
                if wanted in ("Date activité", "Date fin", "Créé le"):
                    val = nice_date(val)
                if wanted == "Durée (jours)" and val:
                    val = f"{val} jour(s)"
                meta_cells.append((wanted, val))
        if meta_cells:
            meta_rows = []
            for offset in range(0, len(meta_cells), 3):
                triplet = meta_cells[offset:offset + 3]
                cells = []
                for label, value in triplet:
                    cells.append(Paragraph(
                        f'<font size="6.5"><b>{_safe_para_text(label)}</b></font><br/>'
                        f'<font size="7.2">{_safe_para_text(value) or "—"}</font>', activity_style
                    ))
                while len(cells) < 3:
                    cells.append(Paragraph("", activity_style))
                meta_rows.append(cells)
            meta_table = Table(meta_rows, colWidths=[page_width / 3] * 3, hAlign="LEFT", splitByRow=1)
            meta_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), fill), ("BOX", (0, 0), (-1, -1), 0.6, border),
                ("INNERGRID", (0, 0), (-1, -1), 0.3, border), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(meta_table)

        # Le reste des champs est présenté un champ = un bloc visuel. Si un texte est
        # long, plusieurs fragments sont placés dans le même tableau avec un libellé
        # unique : plus de « Observation (suite) », « Description (suite) », etc.
        top_level_keys = {str(key).strip().lower().replace("’", "'") for key in row.index
                          if metadata_keys.get(str(key).strip().lower().replace("’", "'")) in metadata_order}
        for column, value in row.items():
            normalized_column = str(column).strip().lower().replace("’", "'")
            if normalized_column in top_level_keys:
                continue
            text_value = clean(value)
            chunks = _split_pdf_text(text_value, max_chars=1000)
            if not chunks:
                chunks = [""]
            field_rows = []
            for part_index, chunk in enumerate(chunks):
                field_rows.append([
                    Paragraph(_safe_para_text(str(column)) if part_index == 0 else "", label_style),
                    Paragraph(_safe_para_text(chunk) or "&nbsp;", activity_style),
                ])
            field_table = Table(field_rows, colWidths=[label_width, value_width], hAlign="LEFT", splitByRow=1, repeatRows=0)
            field_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), fill), ("BOX", (0, 0), (-1, -1), 0.45, border),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(field_table)
        story.append(Spacer(1, 8))


def _append_generic_pdf_cards(story, frame, body_style, section_style, card_prefix="Enregistrement"):
    """Présente tous les registres en fiches cohérentes, sans répéter les libellés des champs longs."""
    if frame is None or frame.empty:
        return
    page_width = 523
    label_width, value_width = 112, 411
    metadata_aliases = {
        "date_activite": "Date activité", "date activité": "Date activité",
        "date_publication": "Date publication", "date_intervention": "Date intervention",
        "date_production": "Date production", "date_visite": "Date visite",
        "date_debut": "Date début", "date_fin": "Date fin", "date fin": "Date fin",
        "date": "Date", "heure_debut": "Heure début", "heure_debut": "Heure début",
        "heure_fin": "Heure fin", "heure fin": "Heure fin", "duree_jours": "Durée (jours)",
        "durée (jours)": "Durée (jours)", "statut": "Statut", "created_at": "Créé le",
        "created at": "Créé le", "updated_at": "Mis à jour le", "updated at": "Mis à jour le",
    }
    metadata_order = ["Date activité", "Date", "Date publication", "Date intervention", "Date production",
                      "Date visite", "Date début", "Date fin", "Durée (jours)", "Heure début", "Heure fin",
                      "Statut", "Créé le", "Mis à jour le"]

    def clean(value):
        if value is None:
            return ""
        value = str(value).strip()
        return "" if value.lower() in ("nan", "none", "nat") else value

    def pretty_date(value):
        value = clean(value)
        try:
            if len(value) >= 10 and value[4] == "-" and value[7] == "-":
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed.strftime("%d/%m/%Y %H:%M" if ("T" in value or " " in value) else "%d/%m/%Y")
        except (ValueError, TypeError):
            pass
        return value

    for index, (_, row) in enumerate(frame.iterrows(), start=1):
        fill_hex, border_hex, text_hex = _activity_color(index)
        fill, border = colors.HexColor(fill_hex), colors.HexColor(border_hex)
        label_style = ParagraphStyle(
            f"GenericCardLabel{index}", parent=body_style, fontName="Helvetica-Bold",
            fontSize=7.2, leading=9, textColor=colors.HexColor(text_hex),
        )
        value_style = ParagraphStyle(
            f"GenericCardValue{index}", parent=body_style, fontSize=7.2, leading=9,
            textColor=colors.HexColor(text_hex), wordWrap="CJK",
        )
        normalized = {str(key).strip().lower().replace("’", "'"): key for key in row.index}
        heading_value = ""
        for candidate in ("titre", "intitulé", "intitule", "nom", "nom_document", "nom document", "objet", "sujet", "libelle", "libellé"):
            if candidate in normalized:
                heading_value = clean(row[normalized[candidate]])
                if heading_value:
                    break
        heading_text = f"{card_prefix} {index}" + (f" — {heading_value}" if heading_value else "")
        heading_style = ParagraphStyle(
            f"GenericCardHeading{index}", parent=section_style, fontSize=10, leading=12,
            textColor=colors.HexColor(text_hex), spaceBefore=0, spaceAfter=0,
        )
        heading = Table([[Paragraph(_safe_para_text(heading_text), heading_style)]], colWidths=[page_width], hAlign="LEFT")
        heading.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), fill), ("BOX", (0, 0), (-1, -1), 0.7, border),
            ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(Spacer(1, 5))
        story.append(heading)

        meta_cells = []
        for wanted in metadata_order:
            actual = next((key for key in row.index if metadata_aliases.get(str(key).strip().lower().replace("’", "'")) == wanted), None)
            if actual is not None:
                val = clean(row[actual])
                if wanted.startswith("Date") or wanted in ("Créé le", "Mis à jour le"):
                    val = pretty_date(val)
                if wanted == "Durée (jours)" and val:
                    val = f"{val} jour(s)"
                if val:
                    meta_cells.append((wanted, val))
        if meta_cells:
            meta_rows = []
            for offset in range(0, len(meta_cells), 3):
                group = meta_cells[offset:offset + 3]
                cells = [Paragraph(
                    f'<font size="6.5"><b>{_safe_para_text(label)}</b></font><br/>'
                    f'<font size="7.2">{_safe_para_text(value)}</font>', value_style
                ) for label, value in group]
                while len(cells) < 3:
                    cells.append(Paragraph("", value_style))
                meta_rows.append(cells)
            meta = Table(meta_rows, colWidths=[page_width / 3] * 3, hAlign="LEFT", splitByRow=1)
            meta.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), fill), ("BOX", (0, 0), (-1, -1), 0.6, border),
                ("INNERGRID", (0, 0), (-1, -1), 0.3, border), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(meta)

        metadata_columns = {str(key).strip().lower().replace("’", "'") for key in row.index
                            if str(key).strip().lower().replace("’", "'") in metadata_aliases}
        for column, value in row.items():
            norm = str(column).strip().lower().replace("’", "'")
            if norm in metadata_columns:
                continue
            chunks = _split_pdf_text(clean(value), max_chars=950)
            field_rows = []
            for part_index, chunk in enumerate(chunks or [""]):
                field_rows.append([
                    Paragraph(_safe_para_text(str(column)) if part_index == 0 else "", label_style),
                    Paragraph(_safe_para_text(chunk) or "&nbsp;", value_style),
                ])
            field_table = Table(field_rows, colWidths=[label_width, value_width], hAlign="LEFT", splitByRow=1)
            field_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), fill), ("BOX", (0, 0), (-1, -1), 0.45, border),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(field_table)
        story.append(Spacer(1, 8))


def _append_pdf_dataset(story, frame, body_style, head_style, section_style, card_prefix="Enregistrement"):
    """Rendu commun aux exports PDF individuels et au rapport complet."""
    if frame is None or frame.empty:
        return
    # Une seule présentation pour tous les registres : les libellés de champs longs
    # restent uniques et toutes les valeurs sont conservées dans la fiche correspondante.
    _append_generic_pdf_cards(story, frame, body_style, section_style, card_prefix=card_prefix)

def pdf_bytes(data, title="Rapport CRA/ISRA"):
    """Génère un PDF robuste : pages découpées, colonnes paginées et repli sécurisé."""
    if not REPORTLAB_AVAILABLE:
        return None

    bio = io.BytesIO()
    doc = SimpleDocTemplate(
        bio, pagesize=A4, rightMargin=34, leftMargin=34,
        topMargin=82, bottomMargin=42, title=str(title)[:200], author="CRA/ISRA",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "BrandTitle", parent=styles["Title"], alignment=TA_LEFT,
        fontSize=20, leading=24, textColor=colors.HexColor("#083B66"), spaceAfter=7,
    )
    subtitle = ParagraphStyle(
        "Subtitle", parent=styles["BodyText"], fontSize=8.5, leading=12,
        textColor=colors.HexColor("#64748B"), spaceAfter=12,
    )
    body = ParagraphStyle(
        "PDFBody", parent=styles["BodyText"], fontSize=7.5, leading=9,
        textColor=colors.HexColor("#1E293B"), wordWrap="CJK",
    )
    head = ParagraphStyle(
        "PDFHead", parent=body, textColor=colors.white,
        fontName="Helvetica-Bold", fontSize=6.5, leading=7.2, wordWrap="CJK",
    )
    section = ParagraphStyle(
        "Section", parent=styles["Heading2"], fontSize=12, leading=15,
        textColor=colors.HexColor("#0B74B8"), spaceBefore=8, spaceAfter=7,
    )

    story = [
        Paragraph("RAPPORT • CRA / ISRA", title_style),
        Paragraph(_safe_para_text(title), section),
        Paragraph(
            "<b>CRA/ISRA Saint-Louis</b> — Registre interne IST et Communication"
            f"<br/>Généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')}", subtitle,
        ),
    ]

    if data is None or data.empty:
        story.append(Paragraph("Aucune donnée à afficher.", body))
    else:
        # Les tables extrêmement larges sont séparées en groupes de colonnes.
        # Pour les textes longs, on évite complètement le tableau : même si ReportLab
        # sait découper certaines lignes, le calcul d'une cellule gigantesque peut
        # lui-même provoquer un LayoutError ou ralentir fortement la génération.
        frame = data.copy().fillna("")
        max_cell_length = 0
        if not frame.empty:
            try:
                max_cell_length = max(
                    len(str(value))
                    for value in frame.astype(str).to_numpy().ravel()
                )
            except (TypeError, ValueError):
                max_cell_length = 0

        if _is_activity_dataset(frame, title):
            story.append(Paragraph(
                "Chaque activité est présentée dans une fiche avec sa propre couleur pastel "
                "pour la distinguer des autres activités. La palette se répète après dix couleurs.", subtitle
            ))
            _append_activity_pdf_cards(story, frame, body, section)
        else:
            story.append(Paragraph(
                "Les enregistrements sont présentés séparément. Les champs longs restent regroupés "
                "sous un seul intitulé et peuvent se poursuivre sur plusieurs lignes ou pages.", subtitle
            ))
            _append_generic_pdf_cards(story, frame, body, section, card_prefix="Enregistrement")

    try:
        doc.build(story, onFirstPage=_pdf_header_footer, onLaterPages=_pdf_header_footer)
        return bio.getvalue()
    except Exception as first_error:
        # Deuxième passe : rapport linéaire sans tableau. Elle protège l'application
        # si une cellule atypique empêche malgré tout ReportLab de découper une table.
        bio = io.BytesIO()
        fallback_doc = SimpleDocTemplate(
            bio, pagesize=A4, rightMargin=34, leftMargin=34,
            topMargin=82, bottomMargin=42, title=str(title)[:200], author="CRA/ISRA",
        )
        fallback_story = [
            Paragraph("RAPPORT • CRA / ISRA", title_style),
            Paragraph(_safe_para_text(title), section),
            Paragraph(
                f"Export de secours généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')}. "
                "Le format tabulaire a été remplacé par une présentation détaillée pour assurer la lecture.",
                subtitle,
            ),
        ]
        if data is None or data.empty:
            fallback_story.append(Paragraph("Aucune donnée à afficher.", body))
        else:
            fallback_frame = data.fillna("")
            if _is_activity_dataset(fallback_frame, title):
                _append_activity_pdf_cards(fallback_story, fallback_frame, body, section)
            else:
                _append_generic_pdf_cards(
                    fallback_story, fallback_frame, body, section, card_prefix="Enregistrement"
                )
        try:
            fallback_doc.build(
                fallback_story, onFirstPage=_pdf_header_footer, onLaterPages=_pdf_header_footer
            )
            return bio.getvalue()
        except Exception as fallback_error:
            # L'erreur est remontée à export_buttons, qui ne doit pas faire planter la page.
            raise RuntimeError(
                f"Export PDF impossible même en mode de secours ({type(fallback_error).__name__})."
            ) from first_error


def _safe_para_text(value):
    """Échappe le texte utilisateur pour qu'il ne soit pas interprété comme du balisage XML."""
    if value is None:
        return ""
    text_value = str(value)
    return (text_value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("\r\n", "<br/>")
            .replace("\n", "<br/>").replace("\r", "<br/>"))


def _split_pdf_text(value, max_chars=1600):
    """Découpe un champ texte massif en fragments sûrs sans supprimer de contenu.

    Les fragments sont séparés de préférence aux espaces ou aux retours à la ligne.
    Cela évite de demander à ReportLab de mettre en page un unique paragraphe énorme
    et permet aux textes de plusieurs centaines de pages de continuer naturellement.
    """
    if value is None:
        return [""]
    text_value = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if not text_value:
        return [""]

    chunks = []
    for source_line in text_value.split("\n"):
        if source_line == "":
            chunks.append("")
            continue
        remaining = source_line
        while len(remaining) > max_chars:
            cut = remaining.rfind(" ", 0, max_chars + 1)
            if cut < max_chars // 2:
                cut = max_chars
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip()
        chunks.append(remaining)
    return chunks or [""]


def _append_long_pdf_text(story, label, value, body_style, label_style=None):
    """Ajoute un champ texte en fragments indépendants, paginables par ReportLab."""
    safe_label = _safe_para_text(label)
    if label_style is not None:
        story.append(Paragraph(f"<b>{safe_label}</b>", label_style))
    else:
        story.append(Paragraph(f"<b>{safe_label}</b>", body_style))

    chunks = _split_pdf_text(value)
    for chunk in chunks:
        # Chaque fragment est un Paragraph distinct : ReportLab peut donc passer à
        # la page suivante entre deux fragments même pour des champs extrêmement longs.
        story.append(Paragraph(_safe_para_text(chunk) or "&nbsp;", body_style))
    story.append(Spacer(1, 4))


def _pdf_report_view(frame, columns, labels=None):
    """Prépare une vue PDF compacte sans supprimer les données originales."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    labels = labels or {}
    available = [c for c in columns if c in frame.columns]
    if not available:
        return frame.copy()
    out = frame.loc[:, available].copy()
    out = out.rename(columns={c: labels.get(c, c) for c in available})
    return out

def complete_report_pdf(report_data, start=None, end=None, report_profile=None):
    """
    RAPPORT PREMIUM — nouvelle structure de l'application.

    Architecture du document :
      01. Synthèse exécutive
      02. Communication
          2.1 Activités — centre de Communication
          2.2 Actions de communication
          2.3 Presse & médias
          2.4 Audiovisuel
          2.5 Formats de communication
      03. Documentations
          3.1 Documentation IST
          3.2 Bibliothèque
          3.3 Consultations
          3.4 Prêts & retours
      04. Résultats scientifiques
          4.1 Fiches techniques
          4.2 Mémoires
          4.3 Thèses
          4.4 Rapports
          4.5 Publications scientifiques & Documentation IST (registre unique)
          4.6 Notes politiques
          4.7 Notes de synthèse
      05. Visiteurs
      06. Valorisation chercheurs
          6.1 Profils chercheurs
          6.2 Actions de valorisation
      07. Profil / identité du responsable

    Cette hiérarchie correspond directement aux espaces de navigation
    de l'application et ne reprend plus l'ancien classement technique.
    """
    if not REPORTLAB_AVAILABLE:
        return None

    report_profile = report_profile or {}

    bio = io.BytesIO()
    doc = SimpleDocTemplate(
        bio,
        pagesize=A4,
        rightMargin=34,
        leftMargin=34,
        topMargin=82,
        bottomMargin=45,
        title="Rapport premium IST et Communication — CRA/ISRA",
        author="CRA/ISRA",
        subject="Rapport structuré Communication • Documentations • Résultats scientifiques",
    )

    styles = getSampleStyleSheet()

    cover_title = ParagraphStyle(
        "PremiumCoverTitle",
        parent=styles["Title"],
        fontSize=25,
        leading=29,
        textColor=colors.HexColor("#062A49"),
        spaceAfter=10,
    )
    cover_sub = ParagraphStyle(
        "PremiumCoverSub",
        parent=styles["BodyText"],
        fontSize=10,
        leading=15,
        textColor=colors.HexColor("#475569"),
        spaceAfter=12,
    )
    section = ParagraphStyle(
        "PremiumSection",
        parent=styles["Heading2"],
        fontSize=15,
        leading=19,
        textColor=colors.HexColor("#083B66"),
        spaceBefore=8,
        spaceAfter=8,
    )
    subsection = ParagraphStyle(
        "PremiumSubsection",
        parent=styles["Heading3"],
        fontSize=10.5,
        leading=13,
        textColor=colors.HexColor("#0B74B8"),
        spaceBefore=8,
        spaceAfter=5,
    )
    body = ParagraphStyle(
        "PremiumBody",
        parent=styles["BodyText"],
        fontSize=8,
        leading=10.5,
        textColor=colors.HexColor("#1E293B"),
        spaceAfter=4,
    )
    small = ParagraphStyle(
        "PremiumSmall",
        parent=body,
        fontSize=7.2,
        leading=9.5,
        textColor=colors.HexColor("#64748B"),
    )
    head = ParagraphStyle(
        "PremiumHead",
        parent=body,
        textColor=colors.white,
        fontName="Helvetica-Bold",
        fontSize=6.5,
        leading=7.5,
    )
    cover_badge = ParagraphStyle(
        "PremiumBadge",
        parent=body,
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#2D8A4A"),
        spaceAfter=10,
    )

    story = []

    # ------------------------------------------------------------
    # COUVERTURE
    # ------------------------------------------------------------
    story.append(Spacer(1, 40))
    story.append(Paragraph("ISRA • CRA", cover_badge))
    story.append(Paragraph("RAPPORT PREMIUM", cover_title))
    story.append(
        Paragraph(
            "IST et Communication",
            ParagraphStyle(
                "CoverBlue",
                parent=cover_title,
                fontSize=19,
                leading=23,
                textColor=colors.HexColor("#0B74B8"),
                spaceAfter=12,
            ),
        )
    )
    story.append(
        Paragraph(
            "Communication • Documentations • Résultats scientifiques",
            cover_sub,
        )
    )
    story.append(
        Table(
            [[
                Paragraph(
                    "<b>Responsable</b><br/>"
                    f"{report_profile.get('nom', 'Ndeye Fota Gueye')}<br/>"
                    f"{report_profile.get('fonction', 'Chargée de la communication et documentation de CRA/ISRA Saint-Louis')}",
                    body,
                ),
                Paragraph(
                    "<b>Période du rapport</b><br/>"
                    f"{start or '—'} → {end or '—'}<br/>"
                    f"Généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')}",
                    body,
                ),
            ]],
            colWidths=[260, 263],
            hAlign="LEFT",
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F8FC")),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#D7E5EF")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ]),
        )
    )
    story.append(Spacer(1, 18))
    story.append(
        Paragraph(
            "Document de pilotage et de capitalisation généré à partir des données "
            "enregistrées dans la plateforme CRA/ISRA.",
            small,
        )
    )
    story.append(PageBreak())

    # ------------------------------------------------------------
    # 01 — SYNTHÈSE EXÉCUTIVE
    # ------------------------------------------------------------
    story.extend(_pdf_section_header(
        "01", "SYNTHÈSE EXÉCUTIVE",
        "Vue d'ensemble alignée sur la nouvelle organisation de l'application.",
        section
    ))

    kpi_order = [
        ("Activités", report_data.get("Activités")),
        ("Compléments de communication — registre unique", report_data.get("Compléments de communication", pd.DataFrame())),
        ("Publications scientifiques & Documentation IST — registre unique", report_data.get("Publications & Documentation IST", pd.DataFrame())),
        ("Bibliothèque", report_data.get("Bibliothèque")),
        ("🧾 Fiches techniques", report_data.get("Fiches techniques", pd.DataFrame())),
        ("📘 Mémoires", report_data.get("Mémoires", pd.DataFrame())),
        ("🎓 Thèses", report_data.get("Thèses", pd.DataFrame())),
        ("📑 Rapports scientifiques", report_data.get("Rapports scientifiques", pd.DataFrame())),
        ("📰 Publications scientifiques & Documentation IST", report_data.get("Publications & Documentation IST", pd.DataFrame())),
        ("🗒️ Notes politiques", report_data.get("Notes politiques", pd.DataFrame())),
        ("📋 Notes de synthèse", report_data.get("Notes de synthèse", pd.DataFrame())),
        ("Résultats scientifiques — total", report_data.get("Résultats scientifiques — regroupés", pd.DataFrame())),
        ("Visiteurs", report_data.get("Visiteurs")),
        ("Valorisation chercheurs", report_data.get("Valorisation chercheurs")),
    ]

    kpis = []
    for label, frame in kpi_order:
        if frame is not None:
            kpis.append((label, len(frame)))

    kpi_table = _pdf_kpi_table(kpis, body)
    if kpi_table is not None:
        story.append(kpi_table)
        story.append(Spacer(1, 12))

    story.append(
        Paragraph(
            "Le présent rapport reprend volontairement la même logique que la navigation "
            "actuelle : les informations sont regroupées par espace métier et non plus "
            "comme une succession de tables techniques. Les catalogues et profils sans "
            "date propre sont présentés comme un état de la base à la date de génération.",
            body,
        )
    )

    # Tableau de synthèse des espaces
    synthesis_rows = [
        ["Espace", "Contenu", "Enregistrements"],
        ["Communication", "Activités (centre) et registre unique des compléments de communication",
         str(len(report_data.get("Activités", pd.DataFrame())) + len(report_data.get("Compléments de communication", pd.DataFrame())))],
        ["Documentations", "Bibliothèque, consultations, prêts/retours et personnes",
         str(sum(len(report_data.get(k, pd.DataFrame())) for k in [
             "Bibliothèque", "Consultations", "Prêts", "Personnes"
         ]))],
        ["Résultats scientifiques", "7 rubriques : fiches techniques, mémoires, thèses, rapports, publications & Documentation IST, notes politiques et notes de synthèse",
         str(sum(len(report_data.get(k, pd.DataFrame())) for k in ["Fiches techniques", "Mémoires", "Thèses", "Rapports scientifiques", "Résultats scientifiques — autres types", "Publications & Documentation IST", "Notes politiques", "Notes de synthèse"]))],
        ["Visiteurs", "Registre des visiteurs",
         str(len(report_data.get("Visiteurs", pd.DataFrame())))],
        ["Valorisation chercheurs", "Profils et actions de valorisation",
         str(sum(len(report_data.get(k, pd.DataFrame())) for k in [
             "Profils chercheurs", "Valorisation chercheurs"
         ]))],
    ]
    synth_df = pd.DataFrame(synthesis_rows[1:], columns=synthesis_rows[0])
    story.append(_pdf_table(synth_df, body, head))
    story.append(PageBreak())

    # ------------------------------------------------------------
    # 02 — COMMUNICATION
    # ------------------------------------------------------------
    story.extend(_pdf_section_header(
        "02", "COMMUNICATION",
        "Activités au centre et un registre unique de compléments de communication réunissant actions, médias, audiovisuel et formats.",
        section
    ))

    # Vues PDF lisibles : les DataFrames originaux restent inchangés pour l'Excel.
    activities_pdf = _pdf_report_view(
        report_data.get("Activités"),
        ["id", "date_activite", "date_fin", "duree_jours", "heure_debut", "heure_fin", "created_at",
         "titre", "type_activite", "objectifs", "description", "heures", "resultats_attendus",
         "lieu", "responsable", "statut", "observations"],
        {"id":"ID", "date_activite":"Date activité", "date_fin":"Date fin", "duree_jours":"Durée (jours)",
         "heure_debut":"Heure début", "heure_fin":"Heure fin", "created_at":"Créé le",
         "titre":"Activité", "type_activite":"Type", "objectifs":"Objectifs",
         "description":"Description", "heures":"Heures", "resultats_attendus":"Résultats attendus",
         "lieu":"Lieu", "responsable":"Responsable", "statut":"Statut", "observations":"Observations"},
    )
    communication_pdf = _pdf_report_view(
        report_data.get("Communication"),
        ["id", "date_publication", "titre", "type_action", "plateforme", "lien", "vues", "reactions", "commentaires", "partages", "telechargements", "observations"],
        {"id":"ID", "date_publication":"Date", "titre":"Action", "type_action":"Type",
         "plateforme":"Plateforme", "lien":"Lien", "vues":"Vues", "reactions":"Réactions",
         "commentaires":"Commentaires", "partages":"Partages", "telechargements":"Téléchargements",
         "observations":"Observations"},
    )
    media_pdf = _pdf_report_view(
        report_data.get("Médias"),
        ["id", "date_intervention", "media_name", "media_type", "journaliste", "personne_interviewee", "sujet", "lieu", "type_intervention", "lien", "observations"],
        {"id":"ID", "date_intervention":"Date", "media_name":"Média", "media_type":"Type média",
         "journaliste":"Journaliste", "personne_interviewee":"Personne interviewée", "sujet":"Sujet",
         "lieu":"Lieu", "type_intervention":"Type intervention", "lien":"Lien", "observations":"Observations"},
    )
    audiovisual_pdf = _pdf_report_view(
        report_data.get("Audiovisuel"),
        ["id", "date_production", "titre", "type_production", "lieu", "theme", "interviewes", "duree", "responsable", "statut", "lien", "observations"],
        {"id":"ID", "date_production":"Date", "titre":"Production", "type_production":"Type",
         "lieu":"Lieu", "theme":"Thème", "interviewes":"Interviewé(s)", "duree":"Durée",
         "responsable":"Responsable", "statut":"Statut", "lien":"Lien", "observations":"Observations"},
    )
    formats_pdf = _pdf_report_view(
        report_data.get("Formats"),
        ["id", "categorie", "nom_format", "description", "usage_recommande", "norme", "lien_reference", "observations"],
        {"id":"ID", "categorie":"Catégorie", "nom_format":"Format", "description":"Description",
         "usage_recommande":"Usage recommandé", "norme":"Norme", "lien_reference":"Référence", "observations":"Observations"},
    )

    communication_sections = [
        ("2.1", "Activités — centre de Communication", "Toutes les activités déjà enregistrées dans la plateforme. Elles constituent le centre de l'espace Communication.", activities_pdf),
        ("2.2", "Compléments de communication — registre unique", "Tableau unifié réunissant actions de communication, presse et médias, productions audiovisuelles et formats.", _pdf_report_view(
            report_data.get("Compléments de communication"),
            ["Origine", "categorie", "titre", "type", "date", "canal", "responsable", "lien", "observations"],
            {"Origine":"Source", "categorie":"Catégorie", "titre":"Titre / contenu", "type":"Type", "date":"Date", "canal":"Canal / thème", "responsable":"Responsable / journaliste", "lien":"Lien web", "observations":"Observations"},
        )),
    ]

    for num, title, desc, frame in communication_sections:
        story.append(Paragraph(f"{num} — {title}", subsection))
        story.append(Paragraph(desc, small))
        if frame is None or frame.empty:
            story.append(Paragraph("Aucune donnée renseignée pour cette rubrique.", body))
        else:
            _append_pdf_dataset(story, frame, body, head, subsection, card_prefix=title)
        story.append(Spacer(1, 6))

    story.append(PageBreak())

    # ------------------------------------------------------------
    # 03 — DOCUMENTATIONS
    # ------------------------------------------------------------
    story.extend(_pdf_section_header(
        "03", "DOCUMENTATIONS",
        "Bibliothèque, consultations, personnes et prêts/retours.",
        section
    ))

    documentation_sections = [
        ("3.1", "Bibliothèque", "État du catalogue et des disponibilités.",
         report_data.get("Bibliothèque")),
        ("3.2", "Consultations", "Historique des consultations.",
         report_data.get("Consultations")),
        ("3.3", "Prêts & retours", "Suivi des emprunts et retours.",
         report_data.get("Prêts")),
        ("3.4", "Personnes", "Personnes enregistrées comme acteurs documentaires.",
         report_data.get("Personnes")),
    ]

    for num, title, desc, frame in documentation_sections:
        story.append(Paragraph(f"{num} — {title}", subsection))
        story.append(Paragraph(desc, small))
        if frame is None or frame.empty:
            story.append(Paragraph("Aucune donnée renseignée pour cette rubrique.", body))
        else:
            _append_pdf_dataset(story, frame, body, head, subsection, card_prefix=title)
        story.append(Spacer(1, 6))

    story.append(PageBreak())

    # ------------------------------------------------------------
    # 04 — PUBLICATIONS & DOCUMENTS SCIENTIFIQUES
    # ------------------------------------------------------------
    story.extend(_pdf_section_header(
        "04", "RÉSULTATS SCIENTIFIQUES",
        "Espace unique de capitalisation des documents scientifiques, publications, notes et ressources IST.",
        section
    ))

    scientific_sections = [
        ("4.1", "🧾 Fiches techniques", "Fiches techniques enregistrées dans le registre des résultats scientifiques.", report_data.get("Fiches techniques")),
        ("4.2", "📘 Mémoires", "Mémoires enregistrés dans le registre des résultats scientifiques.", report_data.get("Mémoires")),
        ("4.3", "🎓 Thèses", "Thèses enregistrées dans le registre des résultats scientifiques.", report_data.get("Thèses")),
        ("4.4", "📑 Rapports", "Rapports scientifiques enregistrés dans le registre des résultats scientifiques.", report_data.get("Rapports scientifiques")),
        ("4.5", "📰 Publications scientifiques & Documentation IST", "Registre unifié présentant ensemble les publications scientifiques et les ressources de Documentation IST.", report_data.get("Publications & Documentation IST")),
        ("4.6", "🗒️ Notes politiques", "Notes politiques enregistrées dans le registre des publications scientifiques.", report_data.get("Notes politiques")),
        ("4.7", "📋 Notes de synthèse", "Notes de synthèse enregistrées dans le registre des publications scientifiques.", report_data.get("Notes de synthèse")),
        ("Annexe", "Autres types de résultats scientifiques", "Enregistrements dont le type ne correspond pas aux six libellés de résultats scientifiques classifiés ; conservés pour garantir l’intégralité des données.", report_data.get("Résultats scientifiques — autres types")),
    ]

    for num, title, desc, frame in scientific_sections:
        story.append(Paragraph(f"{num} — {title}", subsection))
        story.append(Paragraph(desc, small))
        if frame is None or frame.empty:
            story.append(Paragraph("Aucune donnée renseignée pour cette rubrique.", body))
        else:
            _append_pdf_dataset(story, frame, body, head, subsection, card_prefix=title)
        story.append(Spacer(1, 6))

    story.append(PageBreak())

    # ------------------------------------------------------------
    # 05 — VISITEURS
    # ------------------------------------------------------------
    story.extend(_pdf_section_header(
        "05", "VISITEURS",
        "Registre des visites et informations associées.",
        section
    ))

    visitors = report_data.get("Visiteurs")
    if visitors is None or visitors.empty:
        story.append(Paragraph("Aucune visite renseignée pour cette période.", body))
    else:
        _append_pdf_dataset(story, visitors, body, head, subsection, card_prefix="Visiteur")

    story.append(PageBreak())

    # ------------------------------------------------------------
    # 06 — VALORISATION CHERCHEURS
    # ------------------------------------------------------------
    story.extend(_pdf_section_header(
        "06", "VALORISATION CHERCHEURS",
        "Profils des chercheurs et actions de valorisation associées.",
        section
    ))

    for num, title, desc, key in [
        ("6.1", "Profils chercheurs", "Référentiel des chercheurs.", "Profils chercheurs"),
        ("6.2", "Actions de valorisation", "Productions et actions associées aux chercheurs.",
         "Valorisation chercheurs"),
    ]:
        story.append(Paragraph(f"{num} — {title}", subsection))
        story.append(Paragraph(desc, small))
        frame = report_data.get(key)
        if frame is None or frame.empty:
            story.append(Paragraph("Aucune donnée renseignée pour cette rubrique.", body))
        else:
            _append_pdf_dataset(story, frame, body, head, subsection, card_prefix=title)
        story.append(Spacer(1, 6))

    story.append(PageBreak())

    # ------------------------------------------------------------
    # 07 — PROFIL
    # ------------------------------------------------------------
    story.extend(_pdf_section_header(
        "07", "PROFIL",
        "Identité et rôle dans la plateforme.",
        section
    ))

    profile_name = report_profile.get("nom", "Ndeye Fota Gueye")
    profile_function = report_profile.get(
        "fonction",
        "Chargée de la communication et documentation de CRA/ISRA Saint-Louis"
    )

    profile_rows = [
        ["Élément", "Information"],
        ["Nom", profile_name],
        ["Fonction", profile_function],
        ["Périmètre", "Communication • Documentations • Résultats scientifiques • Visiteurs • Valorisation chercheurs"],
        ["Période du rapport", f"{start or '—'} → {end or '—'}"],
        ["Génération", datetime.now().strftime("%d/%m/%Y à %H:%M")],
    ]
    profile_df = pd.DataFrame(profile_rows[1:], columns=profile_rows[0])
    story.append(_pdf_table(profile_df, body, head))
    story.append(Spacer(1, 12))
    story.append(
        Paragraph(
            "Le rapport est généré automatiquement à partir des informations présentes "
            "dans la base PostgreSQL/Supabase de l'application au moment de l'export.",
            small,
        )
    )

    doc.build(
        story,
        onFirstPage=_pdf_header_footer,
        onLaterPages=_pdf_header_footer,
    )
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
        if not REPORTLAB_AVAILABLE:
            st.caption("PDF indisponible : installez reportlab.")
        else:
            try:
                pdf = pdf_bytes(data, title)
                if pdf:
                    st.download_button(
                        "⬇️ PDF", pdf, f"{base_name}.pdf",
                        "application/pdf", key=f"pdf_{base_name}"
                    )
                else:
                    st.caption("Le PDF n'a pas pu être généré. Les exports CSV et Excel restent disponibles.")
            except Exception as exc:
                # Un défaut de mise en page PDF ne doit jamais interrompre l'application.
                st.warning(
                    "L'export PDF a rencontré un problème de mise en page. "
                    "Les exports CSV et Excel restent disponibles. "
                    f"Détail technique : {type(exc).__name__}."
                )

# -----------------------------
# MODIFICATION / SUPPRESSION TRANSVERSALE DES ENREGISTREMENTS
# -----------------------------
CRUD_TABLES_BY_PAGE = {
    "📣 Communication": {
        "Activités": "activities", "Actions de communication": "communications",
        "Presse et médias": "media", "Audiovisuel": "audiovisual",
        "Formats de communication": "communication_formats",
    },
    "📚 Documentations": {
        "Catalogue bibliothèque": "library_items", "Personnes": "people",
        "Consultations": "consultations", "Prêts et retours": "loans",
    },
    "👥 Visiteurs": {"Registre des visiteurs": "library_visits"},
    "🔬 Résultats scientifiques": {
        "Résultats scientifiques": "scientific_results", "Publications scientifiques": "scientific_publications",
        "Documentation IST": "documents",
    },
    "🧬 Valorisation chercheurs": {
        "Profils chercheurs": "researcher_profiles", "Actions de valorisation": "researcher_valorization",
    },
}


def render_record_manager(current_page):
    """Éditeur générique sécurisé par liste blanche, permettant modifier/supprimer les lignes existantes."""
    tables = CRUD_TABLES_BY_PAGE.get(current_page, {})
    if not tables:
        return
    with st.expander("🛠️ Modifier ou supprimer un enregistrement existant", expanded=False):
        st.caption("Choisissez une rubrique et un identifiant. Les modifications sont enregistrées directement dans PostgreSQL/Supabase.")
        label = st.selectbox("Rubrique à gérer", list(tables), key=f"crud_table_{current_page}")
        table = tables[label]
        records = df(f'SELECT * FROM "{table}" ORDER BY id DESC LIMIT 500')
        if records.empty:
            st.info("Aucun enregistrement disponible dans cette rubrique.")
            return
        st.dataframe(records, use_container_width=True, hide_index=True)
        ids = records["id"].tolist()
        selected_id = st.selectbox("Enregistrement à modifier", ids, format_func=lambda x: f"ID {x} — " + str(records.loc[records.id == x].iloc[0].get("titre", records.loc[records.id == x].iloc[0].get("nom", records.loc[records.id == x].iloc[0].get("media_name", "Enregistrement")))), key=f"crud_id_{current_page}_{table}")
        row = q(f'SELECT * FROM "{table}" WHERE id=?', (selected_id,), fetch=True)
        if not row:
            st.warning("Cet enregistrement n'existe plus.")
            return
        values = row[0]
        with st.form(f"crud_edit_form_{current_page}_{table}_{selected_id}"):
            edited = {}
            cols = st.columns(2)
            editable = [c for c in values if c not in ("id", "created_at")]
            for i, col in enumerate(editable):
                value = values[col]
                target = cols[i % 2]
                if isinstance(value, bool):
                    edited[col] = target.checkbox(col.replace("_", " ").capitalize(), value=value, key=f"crud_{table}_{selected_id}_{col}")
                elif isinstance(value, (date, datetime)) or (value is not None and col in {"date_activite", "date_fin", "date_publication", "date_intervention", "date_production", "date_visite", "date_consultation", "date_emprunt", "date_retour_prevue", "date_retour_reelle", "date_action"}):
                    try:
                        dv = value.date() if isinstance(value, datetime) else value
                        if not isinstance(dv, date): dv = date.fromisoformat(str(dv)[:10])
                        edited[col] = target.date_input(col.replace("_", " ").capitalize(), value=dv, key=f"crud_{table}_{selected_id}_{col}")
                    except Exception:
                        edited[col] = target.text_input(col.replace("_", " ").capitalize(), value="" if value is None else str(value), key=f"crud_{table}_{selected_id}_{col}")
                elif isinstance(value, int) and not isinstance(value, bool):
                    edited[col] = target.number_input(col.replace("_", " ").capitalize(), value=int(value), step=1, key=f"crud_{table}_{selected_id}_{col}")
                elif isinstance(value, float):
                    edited[col] = target.number_input(col.replace("_", " ").capitalize(), value=float(value), key=f"crud_{table}_{selected_id}_{col}")
                else:
                    text_value = "" if value is None else str(value)
                    if len(text_value) > 90 or "resume" in col or "description" in col or "observation" in col or "biographie" in col or "objectif" in col or "resultat" in col or "profil_complet" in col or "documents_" in col:
                        edited[col] = target.text_area(col.replace("_", " ").capitalize(), value=text_value, key=f"crud_{table}_{selected_id}_{col}", height=100)
                    else:
                        edited[col] = target.text_input(col.replace("_", " ").capitalize(), value=text_value, key=f"crud_{table}_{selected_id}_{col}")
            save_col, delete_col = st.columns(2)
            save = save_col.form_submit_button("💾 Enregistrer les modifications", type="primary", use_container_width=True)
            delete = delete_col.form_submit_button("🗑️ Supprimer cet enregistrement", use_container_width=True)
        if save:
            if table == "activities" and edited.get("date_activite") and edited.get("date_fin") and edited["date_fin"] < edited["date_activite"]:
                st.error("La date de fin doit être égale ou postérieure à la date de début.")
                return
            assignments = ", ".join(f'"{col}"=?' for col in edited)
            params = []
            nullable_numeric = {"activity_id", "person_id", "item_id", "valorization_id", "researcher_id", "annee", "pages", "vues", "reactions", "commentaires", "partages", "telechargements", "exemplaires", "disponibles", "duree_jours"}
            nullable_dates = {"date_activite", "date_fin", "date_publication", "date_intervention", "date_production", "date_visite", "date_consultation", "date_emprunt", "date_retour_prevue", "date_retour_reelle", "date_action"}
            for col, value in edited.items():
                if isinstance(value, (date, datetime)):
                    value = value.isoformat()
                elif isinstance(value, str) and not value.strip() and col in nullable_numeric | nullable_dates:
                    value = None
                elif isinstance(value, str) and not value.strip() and col.endswith("_id"):
                    value = None
                params.append(value)
            params.append(selected_id)
            q(f'UPDATE "{table}" SET {assignments} WHERE id=?', tuple(params))
            st.success("Modifications enregistrées.")
            st.rerun()
        if delete:
            st.session_state[f"confirm_delete_{table}_{selected_id}"] = True
        if st.session_state.get(f"confirm_delete_{table}_{selected_id}"):
            st.warning(f"Confirmez-vous la suppression de l'enregistrement ID {selected_id} ? Cette action peut être irréversible.")
            yes, no = st.columns(2)
            if yes.button("Oui, supprimer définitivement", key=f"confirm_yes_{table}_{selected_id}", type="primary"):
                q(f'DELETE FROM "{table}" WHERE id=?', (selected_id,))
                st.session_state.pop(f"confirm_delete_{table}_{selected_id}", None)
                st.success("Enregistrement supprimé.")
                st.rerun()
            if no.button("Annuler", key=f"confirm_no_{table}_{selected_id}"):
                st.session_state.pop(f"confirm_delete_{table}_{selected_id}", None)
                st.rerun()


# -----------------------------
# HEADER / NAVIGATION XXL — IST ET COMMUNICATION
# -----------------------------
st.markdown("""
<div class="hero" style="padding:34px 38px;border-radius:26px;box-shadow:0 18px 55px rgba(11,79,138,.18);">
<div class="hero-badge">ISRA • CRA • PLATEFORME INTERNE</div>
<h1 style="font-size:2.45rem;letter-spacing:-.02em;">📚 IST et Communication</h1>
<p style="font-size:1.03rem;">Pilotage de la communication, des activités, de l'IST, de la bibliothèque, des visiteurs, de la valorisation scientifique et des résultats de recherche.</p>
</div>
""", unsafe_allow_html=True)

c_logout1, c_logout2 = st.columns([8,1])
with c_logout2:
    if st.button("🚪 Déconnexion", use_container_width=True):
        st.session_state.clear()
        st.rerun()

# Navigation principale volontairement réduite : les fonctions existantes sont
# regroupées dans des espaces métiers et restent accessibles sans suppression.
pages = [
    "🏠 Tableau de bord",
    "📣 Communication",
    "📚 Documentations",
    "🔬 Résultats scientifiques",
    "👥 Visiteurs",
    "🧬 Valorisation chercheurs",
    "👤 Profil",
    "📊 Rapports & exports",
]
page = st.radio("Navigation principale", pages, horizontal=True, label_visibility="collapsed")

# -----------------------------
# RENDERERS CONSERVÉS
# -----------------------------
def render_dashboard():
    today = date.today().isoformat()
    metrics = [
        ("Activités", scalar("SELECT COUNT(*) FROM activities")),
        ("Participants", scalar("SELECT COUNT(*) FROM participants")),
        ("Personnes", scalar("SELECT COUNT(*) FROM people")),
        ("Compléments de communication — registre unique", scalar("SELECT (SELECT COUNT(*) FROM communications) + (SELECT COUNT(*) FROM media) + (SELECT COUNT(*) FROM audiovisual) + (SELECT COUNT(*) FROM communication_formats)")),
        ("Publications scientifiques & Documentation IST", scalar("SELECT (SELECT COUNT(*) FROM documents) + (SELECT COUNT(*) FROM scientific_publications WHERE type_publication NOT IN ('Note politique','Note de synthèse'))")),
        ("Documents bibliothèque", scalar("SELECT COUNT(*) FROM library_items")),
        ("Consultations", scalar("SELECT COUNT(*) FROM consultations")),
        ("🧾 Fiches techniques", scalar("SELECT COUNT(*) FROM scientific_results WHERE LOWER(type_resultat) IN ('fiche technique','fiches techniques')")),
        ("📘 Mémoires", scalar("SELECT COUNT(*) FROM scientific_results WHERE LOWER(type_resultat) IN ('mémoire','memoire','mémoires','memoires')")),
        ("🎓 Thèses", scalar("SELECT COUNT(*) FROM scientific_results WHERE LOWER(type_resultat) IN ('thèse','these','thèses','theses')")),
        ("📑 Rapports scientifiques", scalar("SELECT COUNT(*) FROM scientific_results WHERE LOWER(type_resultat) LIKE '%rapport%'")),
        ("📰 Publications scientifiques & Documentation IST", scalar("(SELECT COUNT(*) FROM documents) + (SELECT COUNT(*) FROM scientific_publications WHERE type_publication NOT IN ('Note politique','Note de synthèse'))")),
        ("🗒️ Notes politiques", scalar("SELECT COUNT(*) FROM scientific_publications WHERE type_publication='Note politique'")),
        ("📋 Notes de synthèse", scalar("SELECT COUNT(*) FROM scientific_publications WHERE type_publication='Note de synthèse'")),
        ("Résultats scientifiques — total", scalar("(SELECT COUNT(*) FROM scientific_results) + (SELECT COUNT(*) FROM scientific_publications) + (SELECT COUNT(*) FROM documents)")),
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
        SELECT TO_CHAR(date_activite, 'YYYY-MM') AS mois,
               COUNT(*) AS nombre
        FROM activities
        WHERE date_activite IS NOT NULL
        GROUP BY TO_CHAR(date_activite, 'YYYY-MM')
        ORDER BY mois
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


def render_activities():
    st.header("➕ Activités")
    st.caption("Une activité peut se dérouler sur une seule journée ou sur plusieurs jours. La durée se met à jour automatiquement lorsque vous changez les dates.")

    # Les champs de dates sont volontairement placés hors du formulaire :
    # Streamlit ne relance pas les formulaires à chaque changement de widget.
    # Cela permet à la durée de se recalculer immédiatement.
    start_key = "activity_start_date"
    end_key = "activity_end_date"
    multi_key = "activity_multi_days"
    if start_key not in st.session_state:
        st.session_state[start_key] = date.today()
    if multi_key not in st.session_state:
        st.session_state[multi_key] = False
    if end_key not in st.session_state:
        st.session_state[end_key] = st.session_state[start_key]

    # Si la date de début dépasse la date de fin mémorisée, réaligner la fin
    # avant de créer le widget (évite une erreur de min_value sur les reruns).
    debut_memorise = st.session_state[start_key]
    fin_memorisee = st.session_state[end_key]
    if fin_memorisee < debut_memorise:
        st.session_state[end_key] = debut_memorise

    c1, c2, c3 = st.columns(3)
    d = c1.date_input("Date de début", key=start_key)
    multi_jours = c2.checkbox("Activité sur plusieurs jours", key=multi_key)
    if multi_jours:
        # Corriger une éventuelle fin antérieure au début avant d'afficher le champ.
        if st.session_state[end_key] < d:
            st.session_state[end_key] = d
        d_fin = c3.date_input("Date de fin", min_value=d, key=end_key)
    else:
        d_fin = d
        c3.text_input("Durée", value="1 jour", disabled=True, key="activity_duration_single")

    duree_jours = max(1, (d_fin - d).days + 1)
    if d_fin < d:
        st.warning("La date de fin doit être égale ou postérieure à la date de début.")
    st.info(f"🗓️ Durée prévue : **{duree_jours} jour(s)**" + (" (jour de début et jour de fin inclus)" if multi_jours else ""))

    with st.form("activity_form"):
        c1, c2, c3 = st.columns(3)
        titre = c1.text_input("Titre *")
        type_a = c2.selectbox("Type", ["Animation scientifique","Atelier","Réunion","Formation","Sensibilisation","Conférence","Séminaire","Forum","Salon","Foire","Journée portes ouvertes","Journée d’investissement humain (JIH)","Visite","Mission","Rencontre partenaires","Communication","Documentation","Bibliothèque","Autre"])
        domaine = c3.text_input("Domaine")
        c1, c2, c3 = st.columns(3)
        hd = c1.text_input("Heure début (facultatif)", placeholder="09:00")
        hf = c2.text_input("Heure fin (facultatif)", placeholder="17:00")
        lieu = c3.text_input("Lieu")
        c1, c2, c3 = st.columns(3)
        region = c1.text_input("Région")
        localite = c2.text_input("Localité")
        responsable = c3.text_input("Responsable")
        statut = st.selectbox("Statut", ["Prévue","En préparation","Réalisée","Reportée","Annulée"])
        objectifs = st.text_area("Objectifs")
        description = st.text_area("Description")
        resultats = st.text_area("Résultats obtenus")
        observations = st.text_area("Observations")
        submitted = st.form_submit_button("💾 Enregistrer", type="primary")
    if submitted:
        # Relire les dates depuis l'état courant au moment de l'enregistrement.
        d = st.session_state[start_key]
        d_fin = st.session_state[end_key] if st.session_state[multi_key] else d
        duree_jours = (d_fin - d).days + 1
        if not titre.strip():
            st.error("Le titre est obligatoire.")
        elif d_fin < d:
            st.error("La date de fin ne peut pas précéder la date de début.")
        else:
            ref = f"ACT-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
            q("""INSERT INTO activities(reference,titre,type_activite,domaine,date_activite,date_fin,heure_debut,heure_fin,
                 lieu,region,localite,responsable,description,objectifs,resultats,observations,statut)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (ref,titre,type_a,domaine,str(d),str(d_fin),hd,hf,lieu,region,localite,responsable,description,objectifs,resultats,observations,statut))
            st.success(f"Activité enregistrée : {ref} — du {d.strftime('%d/%m/%Y')} au {d_fin.strftime('%d/%m/%Y')} ({duree_jours} jour(s)).")
            st.rerun()
    st.divider()
    st.subheader("Activités enregistrées")
    data = df("SELECT *, (date_fin - date_activite + 1) AS duree_jours FROM activities ORDER BY date_activite DESC, id DESC")
    if not data.empty:
        st.dataframe(data, use_container_width=True, hide_index=True)
    else:
        st.info("Aucune activité enregistrée.")
    # Les exports doivent afficher explicitement le type d'activité avec un libellé
    # compréhensible, aussi bien dans les tableaux PDF que dans Excel et CSV.
    export_data = data.rename(columns={"type_activite": "Type d’activité"}).copy()
    export_buttons(export_data, "activites", "Activités CRA/ISRA")


def _communication_actions():
    st.subheader("📣 Actions de communication et diffusion")
    acts=df("SELECT id,reference,titre FROM activities ORDER BY date_activite DESC")
    actmap={f"{r.reference} — {r.titre}":r.id for r in acts.itertuples()} if not acts.empty else {}
    with st.form("comm_form_new"):
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
            q("""INSERT INTO communications(activity_id,titre,type_action,plateforme,date_publication,lien,vues,reactions,commentaires,partages,telechargements,observations)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(actmap.get(activity_label),titre,type_action,plateforme,str(dp),lien,vues,reactions,commentaires,partages,telechargements,observations))
            st.success("Action de communication enregistrée."); st.rerun()
    data=df("SELECT * FROM communications ORDER BY date_publication DESC")
    st.dataframe(data,use_container_width=True,hide_index=True)
    export_buttons(data,"communication","Actions de communication CRA/ISRA")

def _communication_media():
    st.subheader("📰 Presse et médias")
    with st.form("media_form_new"):
        c1,c2,c3=st.columns(3)
        media_name=c1.text_input("Nom du média *"); media_type=c2.selectbox("Type",["Télévision","Radio","Presse écrite","Presse en ligne","Magazine","Site web","Autre"]); journaliste=c3.text_input("Journaliste")
        c1,c2,c3=st.columns(3)
        interviewee=c1.text_input("Personne interviewée"); sujet=c2.text_input("Sujet"); di=c3.date_input("Date",date.today())
        lieu=st.text_input("Lieu"); type_intervention=st.selectbox("Type d'intervention",["Interview","Reportage","Article","Émission","Conférence de presse","Autre"]); lien=st.text_input("Lien web (URL)",placeholder="https://…")
        observations=st.text_area("Observations")
        ok=st.form_submit_button("💾 Enregistrer",type="primary")
    if ok:
        if not media_name.strip(): st.error("Le nom du média est obligatoire.")
        else:
            q("""INSERT INTO media(media_name,media_type,journaliste,personne_interviewee,sujet,date_intervention,lieu,type_intervention,lien,observations)
                 VALUES(?,?,?,?,?,?,?,?,?,?)""",(media_name,media_type,journaliste,interviewee,sujet,str(di),lieu,type_intervention,lien,observations))
            st.success("Intervention média enregistrée."); st.rerun()
    data=df("SELECT * FROM media ORDER BY date_intervention DESC")
    st.dataframe(data,use_container_width=True,hide_index=True)
    export_buttons(data,"medias","Presse et médias CRA/ISRA")

def _communication_audiovisual():
    st.subheader("🎥 Productions audiovisuelles")
    with st.form("av_form_new"):
        c1,c2,c3=st.columns(3)
        titre=c1.text_input("Titre *"); typ=c2.selectbox("Type",["Film","Vidéo","Interview","Reportage","Documentaire","Autre"]); dp=c3.date_input("Date",date.today())
        c1,c2,c3=st.columns(3)
        lieu=c1.text_input("Lieu"); theme=c2.text_input("Thème"); duree=c3.text_input("Durée")
        interviewes=st.text_area("Personnes interviewées")
        responsable=st.text_input("Responsable"); statut=c1.selectbox("Statut",["Prévu","En préparation","En production","Terminé","Diffusé"])
        lien=c2.text_input("🔗 Lien de diffusion / vidéo / plateforme",placeholder="https://…")
        observations=st.text_area("Observations")
        ok=st.form_submit_button("💾 Enregistrer",type="primary")
    if ok:
        if not titre.strip(): st.error("Le titre est obligatoire.")
        else:
            path=None
            q("""INSERT INTO audiovisual(titre,type_production,date_production,lieu,theme,interviewes,duree,responsable,statut,lien,fichier_original,observations)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(titre,typ,str(dp),lieu,theme,interviewes,duree,responsable,statut,lien,path,observations))
            st.success("Production enregistrée."); st.rerun()
    data=df("SELECT * FROM audiovisual ORDER BY date_production DESC")
    st.dataframe(data,use_container_width=True,hide_index=True)
    export_buttons(data,"audiovisuel","Productions audiovisuelles CRA/ISRA")


def _communication_formats():
    st.subheader("🧩 Formats de communication et de production")
    with st.form("format_form"):
        c1,c2=st.columns(2)
        categorie=c1.selectbox("Catégorie",["Communication","Média","Audiovisuel","Documentation","Scientifique","Numérique","Événementiel"])
        nom_format=c2.text_input("Nom du format *")
        description=st.text_area("Description")
        usage=st.text_area("Usage recommandé")
        norme=st.text_input("Norme / dimensions / spécification")
        lien_ref=st.text_input("Lien de référence")
        observations=st.text_area("Observations")
        ok=st.form_submit_button("💾 Enregistrer le format",type="primary")
    if ok:
        if not nom_format.strip(): st.error("Le nom du format est obligatoire.")
        else:
            q("""INSERT INTO communication_formats(categorie,nom_format,description,usage_recommande,norme,lien_reference,observations)
                 VALUES(?,?,?,?,?,?,?)""",(categorie,nom_format,description,usage,norme,lien_ref,observations))
            st.success("Format enregistré."); st.rerun()
    formats=df("SELECT id,categorie,nom_format,description,usage_recommande,norme,lien_reference,observations FROM communication_formats ORDER BY categorie,nom_format")
    st.dataframe(formats,use_container_width=True,hide_index=True)
    export_buttons(formats,"formats_communication","Formats de communication et production")


def render_communication():
    """Espace Communication avec un contenu unifié pour toutes les ressources de communication."""
    st.header("📣 Communication")
    st.caption("Les activités restent le centre de pilotage. Toutes les actions, médias, productions audiovisuelles et formats sont saisis et consultés dans un registre unique.")
    st.markdown(
        '<div class="section-banner"><span>COMMUNICATION</span>'
        '<strong>Activités • Actions • Médias • Audiovisuel • Formats</strong></div>',
        unsafe_allow_html=True,
    )
    st.markdown("## 🗓️ Activités — centre de Communication")
    st.caption("Toutes les activités enregistrées sont gérées ici. Elles peuvent être liées aux actions de communication.")
    render_activities()

    st.markdown("## 📚 Compléments de communication — registre unique")
    st.caption("Un seul formulaire et un seul tableau réunissent les actions de communication, la presse et les médias, les productions audiovisuelles et les formats. Choisissez le type de contenu puis renseignez les informations utiles ; le lien web est privilégié.")
    acts=df("SELECT id,reference,titre FROM activities ORDER BY date_activite DESC")
    actmap={f"{r.reference} — {r.titre}":r.id for r in acts.itertuples()} if not acts.empty else {}
    with st.form("communication_contenu_unique_form"):
        categorie=st.selectbox("Type de contenu",["Action de communication","Presse et médias","Production audiovisuelle","Format de communication"])
        c1,c2,c3=st.columns(3)
        titre=c1.text_input("Titre / nom du contenu *")
        type_contenu=c2.selectbox("Type / format",["Affiche","Invitation","Communiqué","Dossier de presse","Article","Reportage","Interview","Photographie","Vidéo","Film","Documentaire","Brochure","Plaquette","Kakemono","Publication web","Émission","Conférence de presse","Autre"])
        date_contenu=c3.date_input("Date",date.today())
        c1,c2,c3=st.columns(3)
        activity_label=c1.selectbox("Activité liée (facultatif)",["—"]+list(actmap))
        canal=c2.selectbox("Canal / média / catégorie",["Site web","Facebook","LinkedIn","YouTube","WhatsApp","Radio","Télévision","Presse écrite","Presse en ligne","Magazine","Communication","Média","Audiovisuel","Documentation","Scientifique","Numérique","Événementiel","Autre"])
        statut=c3.selectbox("Statut",["Prévu","En préparation","En production","Terminé","Diffusé","Disponible","Archivé"])
        c1,c2,c3=st.columns(3)
        contact=c1.text_input("Journaliste / responsable / personne interviewée")
        theme=c2.text_input("Sujet / thème")
        lieu=c3.text_input("Lieu")
        c1,c2,c3=st.columns(3)
        duree=c1.text_input("Durée / spécification (facultatif)")
        nom_format=c2.text_input("Nom du format (si applicable)")
        norme=c3.text_input("Norme / dimensions (si applicable)")
        lien=st.text_input("🔗 Lien web / lien de diffusion / référence",placeholder="https://…")
        c1,c2,c3=st.columns(3)
        vues=c1.number_input("Vues",0,step=1)
        reactions=c2.number_input("Réactions",0,step=1)
        partages=c3.number_input("Partages",0,step=1)
        c1,c2=st.columns(2)
        commentaires=c1.number_input("Commentaires",0,step=1)
        telechargements=c2.number_input("Téléchargements",0,step=1)
        description=st.text_area("Description / usage recommandé")
        observations=st.text_area("Observations")
        ok=st.form_submit_button("💾 Enregistrer dans le registre de communication",type="primary")
    if ok:
        if not titre.strip():
            st.error("Le titre ou nom du contenu est obligatoire.")
        else:
            try:
                if categorie == "Action de communication":
                    q("""INSERT INTO communications(activity_id,titre,type_action,plateforme,date_publication,lien,vues,reactions,commentaires,partages,telechargements,observations)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(actmap.get(activity_label),titre,type_contenu,canal,str(date_contenu),lien,vues,reactions,commentaires,partages,telechargements,observations))
                elif categorie == "Presse et médias":
                    q("""INSERT INTO media(media_name,media_type,journaliste,personne_interviewee,sujet,date_intervention,lieu,type_intervention,lien,observations)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""",(titre,canal,contact,contact,theme,str(date_contenu),lieu,type_contenu,lien,observations))
                elif categorie == "Production audiovisuelle":
                    q("""INSERT INTO audiovisual(titre,type_production,date_production,lieu,theme,interviewes,duree,responsable,statut,lien,fichier_original,observations)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(titre,type_contenu,str(date_contenu),lieu,theme,contact,duree,contact,statut,lien,None,observations))
                else:
                    q("""INSERT INTO communication_formats(categorie,nom_format,description,usage_recommande,norme,lien_reference,observations)
                         VALUES(?,?,?,?,?,?,?)""",(canal,nom_format.strip() or titre,description,description,norme,lien,observations))
                st.success("Contenu enregistré dans le registre unique de communication.")
                st.rerun()
            except Exception as exc:
                st.error(f"Impossible d'enregistrer le contenu : {exc}")

    # Vue normalisée unique : les tables historiques restent utilisées en arrière-plan
    # pour préserver la compatibilité avec la base de données existante.
    data=df("""
        SELECT id, 'Action de communication' AS categorie, titre, type_action AS type,
               date_publication::text AS date, plateforme AS canal, NULL::text AS responsable,
               lien, observations FROM communications
        UNION ALL
        SELECT id, 'Presse et médias' AS categorie, media_name AS titre,
               type_intervention AS type, date_intervention::text AS date,
               media_type AS canal, journaliste AS responsable, lien, observations FROM media
        UNION ALL
        SELECT id, 'Production audiovisuelle' AS categorie, titre, type_production AS type,
               date_production::text AS date, theme AS canal, responsable, lien, observations FROM audiovisual
        UNION ALL
        SELECT id, 'Format de communication' AS categorie, nom_format AS titre,
               categorie AS type, NULL::text AS date, norme AS canal,
               NULL::text AS responsable, lien_reference AS lien, observations FROM communication_formats
        ORDER BY date DESC NULLS LAST, titre
    """)
    st.subheader("📋 Registre unique — tous les contenus de communication")
    st.dataframe(data,use_container_width=True,hide_index=True)
    export_buttons(data,"complements_communication_unifies","Compléments de communication — registre unique CRA/ISRA")

def render_documentations():
    """Espace Documentation organisé en quatre onglets métier indépendants."""
    st.header("📚 Documentations")
    st.caption("Gestion séparée de la bibliothèque, des consultations, des personnes et des prêts/retours.")
    tab_biblio, tab_consult, tab_people, tab_loans = st.tabs([
        "📚 Biblio", "📖 Consultations", "👥 Personnes", "📕 Prêts & retours"
    ])

    with tab_biblio:
        st.subheader("📚 Catalogue et gestion des documents")
        with st.form("lib_form_new"):
            c1,c2,c3=st.columns(3)
            inventaire=c1.text_input("N° inventaire"); cote=c2.text_input("Cote"); isbn=c3.text_input("ISBN")
            titre=c1.text_input("Titre *"); sous_titre=c2.text_input("Sous-titre"); auteurs=c3.text_input("Auteur(s)")
            c1,c2,c3=st.columns(3)
            editeur=c1.text_input("Éditeur"); annee=c2.number_input("Année",0,2100,value=date.today().year); typ=c3.selectbox("Type",["Livre","Article","Rapport","Mémoire","Thèse","Document technique","Publication scientifique","Brochure","Plaquette","Revue","Bulletin","Actes de conférence","Document audiovisuel","Autre"])
            c1,c2,c3=st.columns(3)
            domaine=c1.text_input("Domaine"); theme=c2.text_input("Thématique"); mots=c3.text_input("Mots-clés")
            c1,c2,c3=st.columns(3)
            exemplaires=c1.number_input("Exemplaires",1,10000,1); disponibles=c2.number_input("Disponibles",0,10000,1); localisation=c3.text_input("Localisation")
            etat=c1.selectbox("État",["Bon","Moyen","À restaurer","Endommagé"]); format_doc=c2.selectbox("Format",["Papier","Numérique","Papier + numérique"]); lien=c3.text_input("Lien web (URL)",placeholder="https://…")
            resume=st.text_area("Résumé")
            observations=st.text_area("Observations")
            ok=st.form_submit_button("💾 Ajouter au catalogue",type="primary")
        if ok:
            try:
                path=None
                q("""INSERT INTO library_items(inventaire,cote,isbn,titre,sous_titre,auteurs,editeur,annee,type_document,domaine,thematique,mots_cles,exemplaires,disponibles,localisation,etat,format_document,resume,fichier,lien,observations)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(inventaire,cote,isbn,titre,sous_titre,auteurs,editeur,annee,typ,domaine,theme,mots,exemplaires,disponibles,localisation,etat,format_doc,resume,path,lien,observations))
                st.success("Document ajouté au catalogue."); st.rerun()
            except Exception as exc:
                if 'duplicate key' in str(exc).lower() or 'unique constraint' in str(exc).lower(): st.error("Le numéro d'inventaire existe déjà.")
                else: st.error(f"Erreur PostgreSQL / Storage : {exc}")
        catalog=df("SELECT id,inventaire,cote,titre,auteurs,annee,type_document,exemplaires,disponibles,localisation,etat FROM library_items ORDER BY titre")
        st.dataframe(catalog,use_container_width=True,hide_index=True)
        export_buttons(catalog,"catalogue_bibliotheque","Catalogue de la bibliothèque CRA/ISRA")

    with tab_consult:
        st.subheader("📖 Consultations des documents")
        people=options_people(); items=options_items()
        with st.form("consult_form_new"):
            person=st.selectbox("Personne",["—"]+list(people)); item=st.selectbox("Document",["—"]+list(items))
            c1,c2=st.columns(2); dc=c1.date_input("Date",date.today()); heure=c2.text_input("Heure")
            typ_cons=c1.selectbox("Type",["Consultation sur place","Consultation numérique","Autre"]); observations=c2.text_input("Observations")
            ok=st.form_submit_button("💾 Enregistrer la consultation",type="primary")
        if ok:
            q("""INSERT INTO consultations(person_id,item_id,date_consultation,heure,type_consultation,observations)
                 VALUES(?,?,?,?,?,?)""",(people.get(person),items.get(item),str(dc),heure,typ_cons,observations))
            st.success("Consultation enregistrée."); st.rerun()
        consultations_data=df("""SELECT c.id,c.date_consultation,p.nom||' '||COALESCE(p.prenom,'') AS personne,i.titre,c.type_consultation,c.observations
                 FROM consultations c LEFT JOIN people p ON p.id=c.person_id LEFT JOIN library_items i ON i.id=c.item_id
                 ORDER BY c.date_consultation DESC""")
        st.dataframe(consultations_data,use_container_width=True,hide_index=True)
        export_buttons(consultations_data,"consultations","Consultations des documents CRA/ISRA")

    with tab_people:
        st.subheader("👥 Personnes")
        with st.form("person_form_new"):
            c1,c2,c3=st.columns(3)
            nom=c1.text_input("Nom *"); prenom=c2.text_input("Prénom"); fonction=c3.text_input("Fonction")
            c1,c2,c3=st.columns(3)
            structure=c1.text_input("Structure / institution"); categorie=c2.selectbox("Catégorie",["Chercheur","Enseignant-chercheur","Technicien","Personnel administratif","Personnel CRA/ISRA","Étudiant","Stagiaire","Producteur","Organisation de producteurs","Partenaire technique","Partenaire financier","Institution publique","Décideur","Journaliste","Média","ONG","Organisation professionnelle","Visiteur","Communauté locale","Jeune","Grand public","Autre"]); localite=c3.text_input("Localité")
            c1,c2,c3=st.columns(3); telephone=c1.text_input("Téléphone"); email=c2.text_input("Email"); region=c3.text_input("Région")
            observations=st.text_area("Observations")
            ok=st.form_submit_button("💾 Ajouter la personne",type="primary")
        if ok:
            if not nom.strip(): st.error("Le nom est obligatoire.")
            else:
                q("""INSERT INTO people(nom,prenom,fonction,structure,categorie,telephone,email,localite,region,observations)
                     VALUES(?,?,?,?,?,?,?,?,?,?)""",(nom,prenom,fonction,structure,categorie,telephone,email,localite,region,observations))
                st.success("Personne ajoutée."); st.rerun()
        data=df("SELECT id,nom,prenom,fonction,structure,categorie,telephone,email,localite,region FROM people ORDER BY nom")
        st.dataframe(data,use_container_width=True,hide_index=True)
        export_buttons(data,"personnes","Registre des personnes CRA/ISRA")

    with tab_loans:
        st.subheader("📕 Prêts, retours et échéances")
        people=options_people(); items=options_items()
        with st.form("loan_form_new"):
            person=st.selectbox("Emprunteur",["—"]+list(people)); item=st.selectbox("Document",["—"]+list(items))
            c1,c2=st.columns(2); de=c1.date_input("Date d'emprunt",date.today()); dr=c2.date_input("Date prévue de retour",date.today()+timedelta(days=14))
            observations=st.text_area("Observations")
            ok=st.form_submit_button("📕 Enregistrer le prêt",type="primary")
        if ok:
            iid=items.get(item); dispo=scalar("SELECT disponibles FROM library_items WHERE id=?",(iid,)) if iid else 0
            if not people.get(person) or not iid: st.error("Sélectionnez l'emprunteur et le document.")
            elif dispo <= 0: st.error("Aucun exemplaire disponible.")
            else:
                q("""INSERT INTO loans(person_id,item_id,date_emprunt,date_retour_prevue,statut,observations)
                     VALUES(?,?,?,?,?,?)""",(people.get(person),iid,str(de),str(dr),"En cours",observations))
                q("UPDATE library_items SET disponibles=disponibles-1 WHERE id=?",(iid,)); st.success("Prêt enregistré."); st.rerun()
        loans=df("""SELECT l.id,p.nom||' '||COALESCE(p.prenom,'') AS emprunteur,i.titre,i.inventaire,l.date_emprunt,l.date_retour_prevue,l.date_retour_reelle,l.statut,l.observations
                FROM loans l LEFT JOIN people p ON p.id=l.person_id LEFT JOIN library_items i ON i.id=l.item_id ORDER BY l.date_retour_prevue""")
        if not loans.empty:
            q("UPDATE loans SET statut='En retard' WHERE statut='En cours' AND date_retour_prevue < ?",(date.today().isoformat(),))
            loans=df("""SELECT l.id,p.nom||' '||COALESCE(p.prenom,'') AS emprunteur,i.titre,i.inventaire,l.date_emprunt,l.date_retour_prevue,l.date_retour_reelle,l.statut,l.observations
                FROM loans l LEFT JOIN people p ON p.id=l.person_id LEFT JOIN library_items i ON i.id=l.item_id ORDER BY l.date_retour_prevue""")
        st.dataframe(loans,use_container_width=True,hide_index=True)
        export_buttons(loans,"prets_retours","Registre des prêts et retours CRA/ISRA")
        active=df("""SELECT l.id,p.nom||' '||COALESCE(p.prenom,'') AS emprunteur,i.titre,l.date_retour_prevue,l.statut
              FROM loans l LEFT JOIN people p ON p.id=l.person_id LEFT JOIN library_items i ON i.id=l.item_id
              WHERE l.statut IN ('En cours','En retard') ORDER BY l.date_retour_prevue""")
        if not active.empty:
            labels={f"#{r.id} — {r.emprunteur} — {r.titre} — retour prévu {r.date_retour_prevue}":r.id for r in active.itertuples()}
            chosen=st.selectbox("Prêt à retourner",list(labels)); retour=st.date_input("Date réelle de retour",date.today(),key="return_date_new")
            if st.button("↩️ Valider le retour",type="primary"):
                lid=labels[chosen]; iid=scalar("SELECT item_id FROM loans WHERE id=?",(lid,))
                q("UPDATE loans SET date_retour_reelle=?,statut='Retourné' WHERE id=?",(str(retour),lid)); q("UPDATE library_items SET disponibles=disponibles+1 WHERE id=?",(iid,))
                st.success("Retour enregistré."); st.rerun()
        else: st.info("Aucun prêt en cours.")

    st.info("Les exports PDF utilisent un format A4 portrait, avec couverture graphique, en-têtes/pieds de page et toutes les données des registres exportés.")


def render_documentation_ist():
    """Formulaire Documentation IST, conservé pour compatibilité interne."""
    st.subheader("📄 Documentation IST")
    st.caption("Recherche, veille, ressources documentaires et fonds IST.")
    with st.form("doc_form_new"):
        c1,c2,c3=st.columns(3)
        titre=c1.text_input("Titre *"); auteurs=c2.text_input("Auteur(s)"); annee=c3.number_input("Année",1900,2100,value=date.today().year)
        c1,c2,c3=st.columns(3)
        typ=c1.selectbox("Type",["Article scientifique","Mémoire","Thèse","Rapport","Communication","Publication","Document technique","Guide","Brochure","Plaquette","Revue","Bulletin","Autre"])
        theme=c2.text_input("Thématique"); mots=c3.text_input("Mots-clés")
        chercheur=st.text_input("Chercheur associé"); projet=st.text_input("Projet")
        resume=st.text_area("Résumé")
        c1,c2,c3=st.columns(3)
        langue=c1.text_input("Langue",value="Français"); pages=c2.number_input("Pages",0,step=1); reference=c3.text_input("Référence")
        lien=st.text_input("Lien web (URL)",placeholder="https://…")
        statut=st.selectbox("Statut",["Disponible","À vérifier","Archivé"])
        observations=st.text_area("Observations")
        ok=st.form_submit_button("💾 Ajouter le document",type="primary")
    if ok:
        if not titre.strip():
            st.error("Le titre est obligatoire.")
        else:
            path=None
            q("""INSERT INTO documents(titre,auteurs,annee,type_document,thematique,mots_cles,chercheur_associe,projet,resume,langue,pages,reference,fichier,lien,statut,observations)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(titre,auteurs,annee,typ,theme,mots,chercheur,projet,resume,langue,pages,reference,path,lien,statut,observations))
            st.success("Document IST enregistré."); st.rerun()
    data=df("SELECT id,titre,auteurs,annee,type_document,thematique,reference,statut FROM documents ORDER BY annee DESC,id DESC")
    st.dataframe(data,use_container_width=True,hide_index=True)
    export_buttons(data,"documentation_ist","Documentation IST CRA/ISRA")


def render_visitors():
    st.header("👥 Registre des visiteurs")
    st.caption("Registre complet des visites : identité, structure, motif, horaires et liens utiles, sans dépendre du module Personnes.")
    st.markdown('<div class="section-banner"><span>VISITEURS</span><strong>Un registre autonome, riche et centré sur les informations utiles</strong></div>', unsafe_allow_html=True)
    with st.form("visit_form_xxl", clear_on_submit=True):
        c1,c2,c3=st.columns(3)
        nom_vis=c1.text_input("Nom et prénom du visiteur *")
        structure_vis=c2.text_input("Structure / institution")
        categorie_vis=c3.selectbox("Catégorie",["Chercheur","Étudiant","Partenaire","Administration","Entreprise","Média","Visiteur institutionnel","Autre"])
        c1,c2,c3=st.columns(3)
        dv=c1.date_input("Date de visite",date.today())
        ha=c2.text_input("Heure d'arrivée")
        hd=c3.text_input("Heure de départ")
        motif=st.text_input("Motif de la visite")
        c1,c2,c3=st.columns(3)
        contact=c1.text_input("Téléphone / contact")
        email=c2.text_input("E-mail")
        lien=c3.text_input("🔗 Lien utile / profil / institution")
        c1,c2=st.columns(2)
        docs=c1.text_area("Ressources / documents consultés (références ou liens, pas de fichier)")
        emprunts=c2.text_area("Documents empruntés / références")
        observations=st.text_area("Observations")
        ok=st.form_submit_button("💾 Enregistrer la visite",type="primary")
    if ok:
        if not nom_vis.strip():
            st.error("Le nom du visiteur est obligatoire.")
        else:
            q("""INSERT INTO library_visits(person_id,date_visite,heure_arrivee,heure_depart,motif,documents_consultes,documents_empruntes,observations,visitor_name,visitor_structure,visitor_categorie,visitor_contact,visitor_email,visitor_lien)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(None,str(dv),ha,hd,motif,docs,emprunts,observations,nom_vis,structure_vis,categorie_vis,contact,email,lien))
            st.success("Visite enregistrée."); st.rerun()
    visits=df("""SELECT v.id,v.date_visite,COALESCE(v.visitor_name,TRIM(COALESCE(p.nom,'')||' '||COALESCE(p.prenom,''))) AS visiteur,
                 COALESCE(v.visitor_structure,p.structure) AS structure,COALESCE(v.visitor_categorie,p.categorie) AS categorie,
                 v.visitor_contact,v.visitor_email,v.visitor_lien,v.heure_arrivee,v.heure_depart,v.motif,v.documents_consultes,v.documents_empruntes,v.observations
                 FROM library_visits v LEFT JOIN people p ON p.id=v.person_id
                 ORDER BY v.date_visite DESC,v.id DESC""")
    st.subheader("📋 Historique des visites")
    if visits.empty:
        st.info("Aucune visite enregistrée pour le moment. Utilisez le formulaire ci-dessus pour créer le premier enregistrement.")
    else:
        st.dataframe(visits,use_container_width=True,hide_index=True)
        export_buttons(visits,"visiteurs","Registre des visiteurs CRA/ISRA")
        c1,c2=st.columns(2)
        c1.metric("Nombre total de visites",len(visits))
        c2.metric("Visiteurs / structures renseignés",visits["structure"].notna().sum() if "structure" in visits.columns else 0)

def render_researchers():
    st.header("🔬 Valorisation des chercheurs")
    st.caption("Profils chercheurs indépendants de la table Personnes : un chercheur peut être créé ici sans être renseigné dans Documentation.")
    st.markdown('<div class="section-banner"><span>CHERCHEURS</span><strong>Profil • expertise • productions • valorisation</strong></div>', unsafe_allow_html=True)
    p1,p2=st.tabs(["👨‍🔬 Profils des chercheurs","🚀 Actions de valorisation"])
    with p1:
        st.subheader("👨‍🔬 Renseigner un ou plusieurs chercheurs")
        with st.form("researcher_profile_form", clear_on_submit=True):
            c1,c2,c3=st.columns(3)
            nom=c1.text_input("Nom *"); prenom=c2.text_input("Prénom"); fonction=c3.text_input("Grade / fonction")
            c1,c2,c3=st.columns(3)
            unite=c1.text_input("Unité / laboratoire"); domaine=c2.text_input("Domaine d’expertise"); mots=c3.text_input("Mots-clés")
            c1,c2,c3=st.columns(3)
            email=c1.text_input("E-mail"); telephone=c2.text_input("Téléphone"); orcid=c3.text_input("ORCID")
            bio=st.text_area("Biographie courte")
            profil=st.text_area("Profil scientifique complet",height=150)
            lien=st.text_input("🔗 Lien du profil / page chercheur",help="Privilégier un lien stable vers ORCID, Google Scholar, page institutionnelle, laboratoire ou publication.")
            observations=st.text_area("Observations")
            ok=st.form_submit_button("💾 Créer le profil chercheur",type="primary")
        if ok:
            if not nom.strip(): st.error("Le nom du chercheur est obligatoire.")
            else:
                q("""INSERT INTO researcher_profiles(nom,prenom,grade_fonction,unite_laboratoire,domaine_expertise,mots_cles,biographie,profil_complet,email,telephone,ORCID,lien_profil,observations) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(nom,prenom,fonction,unite,domaine,mots,bio,profil,email,telephone,orcid,lien,observations))
                st.success("Profil chercheur créé indépendamment de Documentation."); st.rerun()
        profiles=df("SELECT id,nom,prenom,grade_fonction,unite_laboratoire,domaine_expertise,email,ORCID,lien_profil FROM researcher_profiles ORDER BY nom,prenom")
        st.dataframe(profiles,use_container_width=True,hide_index=True)
        export_buttons(profiles,"profils_chercheurs","Profils des chercheurs CRA/ISRA")
    with p2:
        profiles=df("SELECT id,nom,prenom,grade_fonction FROM researcher_profiles ORDER BY nom,prenom")
        pmap={f"{r.nom} {r.prenom or ''} — {r.grade_fonction or ''}".strip():r.id for r in profiles.itertuples()} if not profiles.empty else {}
        st.subheader("🚀 Nouvelle action de valorisation")
        with st.form("researcher_valorization_form_xxl"):
            chercheurs=st.multiselect("Chercheur(s) concerné(s)",list(pmap.keys()),help="Plusieurs chercheurs peuvent être associés à une même action.")
            c1,c2,c3=st.columns(3)
            da=c1.date_input("Date",date.today()); domaine=c2.text_input("Domaine"); theme=c3.text_input("Thématique")
            projet=st.text_input("Projet")
            c1,c2,c3=st.columns(3)
            type_val=c1.selectbox("Type de valorisation",["Article","Interview","Portrait","Vidéo","Publication","Communication scientifique","Conférence","Démonstration","Autre"])
            support=c2.text_input("Support"); lien=c3.text_input("Lien web (URL)",placeholder="https://…")
            observations=st.text_area("Observations")
            ok=st.form_submit_button("💾 Enregistrer la valorisation",type="primary")
        if ok:
            if not chercheurs: st.error("Sélectionnez au moins un chercheur.")
            else:
                q("""INSERT INTO researcher_valorization(person_id,date_action,domaine,thematique,projet,type_valorisation,support,lien,observations) VALUES(?,?,?,?,?,?,?,?,?) RETURNING id""",(None,str(da),domaine,theme,projet,type_val,support,lien,observations))
                vid=scalar("SELECT MAX(id) FROM researcher_valorization")
                for label in chercheurs: q("INSERT INTO researcher_valorization_researchers(valorization_id,researcher_id) VALUES(?,?) ON CONFLICT DO NOTHING",(vid,pmap[label]))
                st.success("Valorisation enregistrée pour le ou les chercheurs sélectionnés."); st.rerun()
        research_data=df("""SELECT rv.id, STRING_AGG(TRIM(COALESCE(rp.prenom,'')||' '||rp.nom), ', ' ORDER BY rp.nom) AS chercheurs, rv.date_action,rv.domaine,rv.thematique,rv.projet,rv.type_valorisation,rv.support,rv.lien FROM researcher_valorization rv LEFT JOIN researcher_valorization_researchers rvr ON rvr.valorization_id=rv.id LEFT JOIN researcher_profiles rp ON rp.id=rvr.researcher_id GROUP BY rv.id ORDER BY rv.date_action DESC""")
        st.dataframe(research_data,use_container_width=True,hide_index=True)
        export_buttons(research_data,"valorisation_chercheurs","Valorisation des chercheurs CRA/ISRA")

def render_profile():
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

def render_scientific_results():
    """Espace Résultats scientifiques organisé en sept onglets métier."""
    st.header("🔬 Résultats scientifiques")
    st.caption(
        "Un espace unique pour les fiches techniques, mémoires, thèses, rapports, "
        "publications scientifiques, documentation IST, notes politiques et notes de synthèse. "
        "Privilégiez les liens web, DOI et liens institutionnels : aucun fichier n'est téléversé."
    )
    st.markdown(
        '<div class="section-banner"><span>RÉSULTATS SCIENTIFIQUES</span>'
        '<strong>Documents • Publications & IST • Notes politiques • Notes de synthèse</strong></div>',
        unsafe_allow_html=True,
    )

    # Sept onglets : Publication scientifique et Documentation IST sont réunies
    # dans un même onglet, sans supprimer leurs formulaires, registres ou exports.
    tab_fiche, tab_memoire, tab_these, tab_rapport, tab_publications_ist, tab_note_politique, tab_note_synthese = st.tabs([
        "🧾 Fiches techniques", "📘 Mémoires", "🎓 Thèses", "📑 Rapports",
        "📰 Publications scientifiques & Documentation IST", "🗒️ Notes politiques", "📋 Notes de synthèse"
    ])

    def render_result_form(label):
        with st.form(f"scientific_{label.replace(' ','_')}"):
            c1,c2,c3=st.columns(3)
            titre=c1.text_input("Titre *", key=f"sci_titre_{label}")
            auteurs=c2.text_input("Auteur(s)", key=f"sci_auteurs_{label}")
            annee=c3.number_input("Année",1900,2100,value=date.today().year,key=f"sci_annee_{label}")
            c1,c2,c3=st.columns(3)
            domaine=c1.text_input("Domaine",key=f"sci_domaine_{label}")
            projet=c2.text_input("Projet",key=f"sci_projet_{label}")
            public_cible=c3.text_input("Public cible",key=f"sci_public_{label}")
            resume=st.text_area("Résumé",key=f"sci_resume_{label}")
            c1,c2,c3=st.columns(3)
            mots=c1.text_input("Mots-clés",key=f"sci_mots_{label}")
            reference=c2.text_input("Référence / DOI",key=f"sci_ref_{label}")
            lien=c3.text_input("Lien web",key=f"sci_lien_{label}",placeholder="https://…")
            statut=st.selectbox("Statut",["Disponible","En préparation","Archivé"],key=f"sci_statut_{label}")
            observations=st.text_area("Observations",key=f"sci_obs_{label}")
            ok=st.form_submit_button("💾 Enregistrer",type="primary")
        if ok:
            if not titre.strip():
                st.error("Le titre est obligatoire.")
            else:
                q("""INSERT INTO scientific_results(type_resultat,titre,auteurs,annee,domaine,projet,resume,mots_cles,public_cible,reference,fichier,lien,statut,observations)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (label,titre,auteurs,annee,domaine,projet,resume,mots,public_cible,reference,None,lien,statut,observations))
                st.success(f"{label} enregistré."); st.rerun()
        data=df("SELECT id,type_resultat,titre,auteurs,annee,domaine,projet,reference,statut,lien FROM scientific_results WHERE type_resultat=? ORDER BY annee DESC,id DESC",(label,))
        st.dataframe(data,use_container_width=True,hide_index=True)
        export_buttons(data,f"{label.lower().replace(' ','_')}",label)

    with tab_fiche:
        st.subheader("🧾 Fiches techniques")
        render_result_form("Fiche technique")
    with tab_memoire:
        st.subheader("📘 Mémoires")
        render_result_form("Mémoire")
    with tab_these:
        st.subheader("🎓 Thèses")
        render_result_form("Thèse")
    with tab_rapport:
        st.subheader("📑 Rapports scientifiques")
        render_result_form("Rapport")

    with tab_publications_ist:
        st.subheader("📰 Publications scientifiques & Documentation IST — registre unique")
        st.caption("Un seul formulaire et un seul tableau regroupent les publications scientifiques et les ressources IST. Le type de ressource détermine le registre historique dans lequel les données sont conservées.")
        with st.form("publications_ist_contenu_unique_form"):
            origine=st.selectbox("Nature de la ressource",["Publication scientifique","Documentation IST"])
            c1,c2,c3=st.columns(3)
            titre=c1.text_input("Titre *",key="pubist_titre")
            auteurs=c2.text_input("Auteur(s)",key="pubist_auteurs")
            annee=c3.number_input("Année",1900,2100,value=date.today().year,key="pubist_annee")
            c1,c2,c3=st.columns(3)
            type_ressource=c1.selectbox("Type de document",["Article scientifique","Ouvrage","Chapitre d’ouvrage","Communication scientifique","Actes de conférence","Rapport","Mémoire","Thèse","Document technique","Guide","Brochure","Plaquette","Revue / bulletin","Autre"],key="pubist_type")
            domaine=c2.text_input("Domaine / thématique",key="pubist_domaine")
            projet=c3.text_input("Projet associé",key="pubist_projet")
            c1,c2,c3=st.columns(3)
            mots=c1.text_input("Mots-clés",key="pubist_mots")
            public_cible=c2.text_input("Public cible / chercheur associé",key="pubist_public")
            langue=c3.text_input("Langue",value="Français",key="pubist_langue")
            resume=st.text_area("Résumé",key="pubist_resume")
            c1,c2,c3=st.columns(3)
            reference=c1.text_input("Référence bibliographique / DOI",key="pubist_reference")
            pages=c2.number_input("Pages (si applicable)",0,step=1,key="pubist_pages")
            statut=c3.selectbox("Statut",["Disponible","À vérifier","En préparation","Archivé"],key="pubist_statut")
            lien=st.text_input("🔗 Lien web / DOI / lien institutionnel",placeholder="https://…",key="pubist_lien")
            observations=st.text_area("Observations",key="pubist_observations")
            ok=st.form_submit_button("💾 Enregistrer dans le registre scientifique & IST",type="primary")
        if ok:
            if not titre.strip():
                st.error("Le titre est obligatoire.")
            else:
                try:
                    if origine == "Publication scientifique":
                        q("""INSERT INTO scientific_publications(type_publication,titre,auteurs,annee,domaine,projet,resume,mots_cles,public_cible,reference,fichier,lien,statut,observations)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(type_ressource,titre,auteurs,annee,domaine,projet,resume,mots,public_cible,reference,None,lien,statut,observations))
                    else:
                        q("""INSERT INTO documents(titre,auteurs,annee,type_document,thematique,mots_cles,chercheur_associe,projet,resume,langue,pages,reference,fichier,lien,statut,observations)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(titre,auteurs,annee,type_ressource,domaine,mots,public_cible,projet,resume,langue,pages,reference,None,lien,statut,observations))
                    st.success("Ressource enregistrée dans le registre unique.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Impossible d'enregistrer la ressource : {exc}")

        # Normalisation des deux registres dans un tableau unique visible et exportable.
        pubs=df("""SELECT id, 'Publication scientifique' AS origine, type_publication AS type,
                       titre,auteurs,annee,domaine,projet,resume,mots_cles,public_cible,reference,lien,statut,observations
                    FROM scientific_publications
                    WHERE type_publication NOT IN ('Note politique','Note de synthèse')""")
        docs=df("""SELECT id, 'Documentation IST' AS origine, type_document AS type,
                       titre,auteurs,annee,thematique AS domaine,projet,resume,mots_cles,
                       chercheur_associe AS public_cible,reference,lien,statut,observations
                    FROM documents""")
        combined=pd.concat([pubs,docs],ignore_index=True,sort=False)
        if not combined.empty and "annee" in combined.columns:
            combined=combined.sort_values(by=["annee","titre"],ascending=[False,True],na_position="last")
        st.subheader("📋 Registre unique — publications et documentation IST")
        st.dataframe(combined,use_container_width=True,hide_index=True)
        export_buttons(combined,"publications_documentation_ist","Publications scientifiques & Documentation IST — registre unique CRA/ISRA")

    def render_publication_note(ptype, icon):
        st.subheader(f"{icon} {ptype}")
        with st.form(f"publication_{ptype.replace(' ','_')}"):
            c1,c2,c3=st.columns(3)
            titre=c1.text_input("Titre *",key=f"{ptype}_titre")
            auteurs=c2.text_input("Auteur(s)",key=f"{ptype}_auteurs")
            annee=c3.number_input("Année",1900,2100,value=date.today().year,key=f"{ptype}_annee")
            c1,c2,c3=st.columns(3)
            domaine=c1.text_input("Domaine",key=f"{ptype}_domaine")
            projet=c2.text_input("Projet",key=f"{ptype}_projet")
            public_cible=c3.text_input("Public cible",key=f"{ptype}_public")
            resume=st.text_area("Résumé",key=f"{ptype}_resume")
            c1,c2,c3=st.columns(3)
            mots=c1.text_input("Mots-clés",key=f"{ptype}_mots")
            reference=c2.text_input("Référence / DOI",key=f"{ptype}_reference")
            lien=c3.text_input("Lien web",key=f"{ptype}_lien",placeholder="https://…")
            statut=st.selectbox("Statut",["Disponible","En préparation","Archivé"],key=f"{ptype}_statut")
            observations=st.text_area("Observations",key=f"{ptype}_observations")
            ok=st.form_submit_button(f"💾 Enregistrer — {ptype}",type="primary")
        if ok:
            if not titre.strip():
                st.error("Le titre est obligatoire.")
            else:
                q("""INSERT INTO scientific_publications(type_publication,titre,auteurs,annee,domaine,projet,resume,mots_cles,public_cible,reference,fichier,lien,statut,observations)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (ptype,titre,auteurs,annee,domaine,projet,resume,mots,public_cible,reference,None,lien,statut,observations))
                st.success(f"{ptype} enregistrée."); st.rerun()
        data=df("SELECT id,type_publication,titre,auteurs,annee,domaine,projet,reference,statut,lien FROM scientific_publications WHERE type_publication=? ORDER BY annee DESC,id DESC",(ptype,))
        st.dataframe(data,use_container_width=True,hide_index=True)
        export_buttons(data,ptype.lower().replace(' ','_'),ptype)

    with tab_note_politique:
        render_publication_note("Note politique", "🗒️")
    with tab_note_synthese:
        render_publication_note("Note de synthèse", "📋")


def render_reports():
    """Centre de rapports : structure identique à la nouvelle navigation."""
    st.header("📊 Rapports, statistiques et exports")
    st.caption(
        "Rapport premium global aligné sur l'organisation actuelle : "
        "Communication • Documentations (Biblio, Consultations, Personnes, Prêts) • Résultats scientifiques (7 rubriques métier distinctes, dont un registre unique Publications scientifiques & Documentation IST) • Visiteurs • Valorisation chercheurs. "
        "La période proposée commence en 1900 afin d'inclure l'historique complet par défaut ; les lignes sans date ou année sont également conservées."
    )

    st.markdown("""
    <div class="card" style="background:linear-gradient(135deg,#062A49,#0B74B8 65%,#2D8A4A);
         color:white;border:none;animation:fadeInUp .55s ease-out;">
      <div style="font-size:.72rem;letter-spacing:.15em;opacity:.78;">RAPPORT PREMIUM • CRA / ISRA</div>
      <div style="font-size:1.65rem;font-weight:800;margin-top:6px;">IST et Communication</div>
      <div style="font-size:.95rem;margin-top:5px;opacity:.94;">
        Rapport structuré selon les espaces métiers de la nouvelle application.
      </div>
    </div>
    """, unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    start = c1.date_input(
        "Du",
        date(1900, 1, 1),
        key="report_start_date",
    )
    end = c2.date_input(
        "Au",
        date.today(),
        key="report_end_date",
    )

    if start > end:
        st.error("La date de début doit être antérieure ou égale à la date de fin.")
        return

    # ============================================================
    # DONNÉES DU RAPPORT — STRICTEMENT LIMITÉES À LA PÉRIODE CHOISIE
    # ============================================================
    # Une activité est incluse si sa période chevauche la période du rapport.
    activities = df(
        "SELECT *, (date_fin - date_activite + 1) AS duree_jours FROM activities "
        "WHERE (date_activite IS NULL OR (date_activite <= ? AND COALESCE(date_fin, date_activite) >= ?)) "
        "ORDER BY date_activite, id",
        (str(end), str(start)),
    )

    communications = df(
        "SELECT * FROM communications WHERE date_publication IS NULL OR date_publication BETWEEN ? AND ? "
        "ORDER BY date_publication NULLS LAST, id",
        (str(start), str(end)),
    )
    media_df = df(
        "SELECT * FROM media WHERE date_intervention IS NULL OR date_intervention BETWEEN ? AND ? "
        "ORDER BY date_intervention NULLS LAST, id",
        (str(start), str(end)),
    )
    av = df(
        "SELECT * FROM audiovisual WHERE date_production IS NULL OR date_production BETWEEN ? AND ? "
        "ORDER BY date_production NULLS LAST, id",
        (str(start), str(end)),
    )

    # Les formats, documents du catalogue, ouvrages, personnes et profils sont
    # des référentiels sans date d'activité fiable : ils restent disponibles.
    formats = df(
        "SELECT * FROM communication_formats ORDER BY categorie, nom_format"
    )
    documents = df(
        "SELECT * FROM documents WHERE annee IS NULL OR annee BETWEEN ? AND ? "
        "ORDER BY annee DESC, id DESC",
        (start.year, end.year),
    )
    library = df("SELECT * FROM library_items ORDER BY titre")

    consultations = df(
        "SELECT c.id, c.date_consultation, "
        "p.nom || ' ' || COALESCE(p.prenom,'') AS personne, "
        "i.titre AS document, c.heure, c.type_consultation, c.observations "
        "FROM consultations c "
        "LEFT JOIN people p ON p.id=c.person_id "
        "LEFT JOIN library_items i ON i.id=c.item_id "
        "WHERE c.date_consultation IS NULL OR c.date_consultation BETWEEN ? AND ? "
        "ORDER BY c.date_consultation, c.id",
        (str(start), str(end)),
    )
    loans = df(
        "SELECT l.id, l.date_emprunt, l.date_retour_prevue, l.date_retour_reelle, "
        "l.statut, l.observations, "
        "p.nom || ' ' || COALESCE(p.prenom,'') AS emprunteur, "
        "i.titre AS document "
        "FROM loans l "
        "LEFT JOIN people p ON p.id=l.person_id "
        "LEFT JOIN library_items i ON i.id=l.item_id "
        "WHERE l.date_emprunt IS NULL OR l.date_emprunt BETWEEN ? AND ? "
        "ORDER BY l.date_emprunt, l.id",
        (str(start), str(end)),
    )

    people = df(
        "SELECT id, nom, prenom, fonction, structure, categorie, telephone, email, "
        "localite, region, observations "
        "FROM people ORDER BY nom, prenom"
    )

    visits = df(
        "SELECT v.id, v.date_visite, "
        "COALESCE(v.visitor_name, TRIM(COALESCE(p.nom,'') || ' ' || COALESCE(p.prenom,''))) AS visiteur, "
        "COALESCE(v.visitor_structure,p.structure) AS structure, "
        "COALESCE(v.visitor_categorie,p.categorie) AS categorie, "
        "v.visitor_contact, v.visitor_email, v.visitor_lien, "
        "v.heure_arrivee, v.heure_depart, v.motif, "
        "v.documents_consultes, v.documents_empruntes, v.observations "
        "FROM library_visits v "
        "LEFT JOIN people p ON p.id=v.person_id "
        "WHERE v.date_visite IS NULL OR v.date_visite BETWEEN ? AND ? "
        "ORDER BY v.date_visite, v.id",
        (str(start), str(end)),
    )

    scientific = df(
        "SELECT * FROM scientific_results "
        "WHERE annee IS NULL OR annee BETWEEN ? AND ? "
        "ORDER BY annee DESC NULLS LAST, id DESC",
        (start.year, end.year),
    )

    publications = df(
        "SELECT * FROM scientific_publications "
        "WHERE annee IS NULL OR annee BETWEEN ? AND ? "
        "ORDER BY annee DESC NULLS LAST, id DESC",
        (start.year, end.year),
    )

    profiles = df(
        "SELECT id, nom, prenom, grade_fonction, unite_laboratoire, "
        "domaine_expertise, mots_cles, biographie, profil_complet, email, "
        "telephone, ORCID, lien_profil, observations "
        "FROM researcher_profiles ORDER BY nom, prenom"
    )

    research_data = df("""
        SELECT
            rv.id,
            STRING_AGG(
                TRIM(COALESCE(rp.prenom,'') || ' ' || rp.nom),
                ', ' ORDER BY rp.nom
            ) AS chercheurs,
            rv.date_action,
            rv.domaine,
            rv.thematique,
            rv.projet,
            rv.type_valorisation,
            rv.support,
            rv.lien,
            rv.observations
        FROM researcher_valorization rv
        LEFT JOIN researcher_valorization_researchers rvr
            ON rvr.valorization_id=rv.id
        LEFT JOIN researcher_profiles rp
            ON rp.id=rvr.researcher_id
        WHERE rv.date_action BETWEEN ? AND ?
        GROUP BY rv.id
        ORDER BY rv.date_action, rv.id
    """, (str(start), str(end)))

    # Registre combiné normalisé : une seule structure de colonnes pour les deux sources.
    def _normalized_resource_frame(frame, origin, mapping):
        columns = ["Origine", "Type", "Titre", "Auteurs", "Année", "Domaine / thématique", "Projet",
                   "Résumé", "Mots-clés", "Public cible / chercheur associé", "Langue", "Pages",
                   "Référence / DOI", "Lien web", "Statut", "Observations"]
        if frame is None or frame.empty:
            return pd.DataFrame(columns=columns)
        result = pd.DataFrame(index=frame.index)
        result["Origine"] = origin
        for target, source in mapping.items():
            result[target] = frame[source] if source in frame.columns else ""
        for col in columns:
            if col not in result.columns:
                result[col] = ""
        return result[columns]

    publications_sans_notes = publications[
        ~publications["type_publication"].isin(["Note politique", "Note de synthèse"])
    ].copy() if not publications.empty and "type_publication" in publications.columns else pd.DataFrame()
    publications_ist = pd.concat([
        _normalized_resource_frame(publications_sans_notes, "Publication scientifique", {
            "Type":"type_publication", "Titre":"titre", "Auteurs":"auteurs", "Année":"annee",
            "Domaine / thématique":"domaine", "Projet":"projet", "Résumé":"resume", "Mots-clés":"mots_cles",
            "Public cible / chercheur associé":"public_cible", "Référence / DOI":"reference", "Lien web":"lien",
            "Statut":"statut", "Observations":"observations"}),
        _normalized_resource_frame(documents, "Documentation IST", {
            "Type":"type_document", "Titre":"titre", "Auteurs":"auteurs", "Année":"annee",
            "Domaine / thématique":"thematique", "Projet":"projet", "Résumé":"resume", "Mots-clés":"mots_cles",
            "Public cible / chercheur associé":"chercheur_associe", "Langue":"langue", "Pages":"pages",
            "Référence / DOI":"reference", "Lien web":"lien", "Statut":"statut", "Observations":"observations"}),
    ], ignore_index=True, sort=False)

    def _normalized_communication_frame(frame, origin, mapping):
        columns = ["Origine", "categorie", "titre", "type", "date", "canal", "responsable", "lien", "observations"]
        if frame is None or frame.empty:
            return pd.DataFrame(columns=columns)
        result = pd.DataFrame(index=frame.index)
        result["Origine"] = origin
        for target, source in mapping.items():
            result[target] = frame[source] if source in frame.columns else ""
        for col in columns:
            if col not in result.columns:
                result[col] = ""
        return result[columns]

    communication_unifiee = pd.concat([
        _normalized_communication_frame(communications, "Action de communication", {
            "categorie":"categorie", "titre":"titre", "type":"type_action", "date":"date_publication",
            "canal":"plateforme", "responsable":"", "lien":"lien", "observations":"observations"}),
        _normalized_communication_frame(media_df, "Presse et médias", {
            "categorie":"media_type", "titre":"media_name", "type":"type_intervention", "date":"date_intervention",
            "canal":"sujet", "responsable":"journaliste", "lien":"lien", "observations":"observations"}),
        _normalized_communication_frame(av, "Production audiovisuelle", {
            "categorie":"type_production", "titre":"titre", "type":"type_production", "date":"date_production",
            "canal":"theme", "responsable":"responsable", "lien":"lien", "observations":"observations"}),
        _normalized_communication_frame(formats, "Format de communication", {
            "categorie":"categorie", "titre":"nom_format", "type":"categorie", "date":"", "canal":"norme",
            "responsable":"", "lien":"lien_reference", "observations":"observations"}),
    ], ignore_index=True, sort=False)

    # Répartition explicite des résultats scientifiques selon les 7 onglets métier.
    # Chaque rubrique est présentée séparément dans le rapport premium, le tableau de bord,
    # l'aperçu et les exports. Le registre Publications & Documentation IST reste unifié.
    _scientific_type = scientific.get("type_resultat", pd.Series(index=scientific.index, dtype="object")).fillna("").astype(str).str.strip().str.casefold()
    fiches_techniques = scientific[_scientific_type.isin(["fiche technique", "fiches techniques"])].copy()
    memoires = scientific[_scientific_type.isin(["mémoire", "memoire", "mémoires", "memoires"])].copy()
    theses = scientific[_scientific_type.isin(["thèse", "these", "thèses", "theses"])].copy()
    rapports_scientifiques = scientific[_scientific_type.str.contains("rapport", na=False)].copy()
    # Les libellés non reconnus restent visibles dans le registre global pour éviter toute perte.
    scientific_classified = pd.concat([fiches_techniques, memoires, theses, rapports_scientifiques]).drop_duplicates(subset=["id"] if "id" in scientific.columns else None)
    scientific_autres = scientific[~scientific.index.isin(scientific_classified.index)].copy()

    # ============================================================
    # INDICATEURS — données de la période sélectionnée
    # ============================================================
    st.info(
        f"Le rapport global est filtré sur la période du {start:%d/%m/%Y} "
        f"au {end:%d/%m/%Y}. Les indicateurs, l’aperçu et les exports "
        "reprennent les enregistrements correspondant à cette période, ainsi que les enregistrements sans date/année renseignée afin de ne pas perdre d’anciennes saisies. "
        "Les référentiels sans date (catalogue, formats, personnes et profils) "
        "restent disponibles comme données de référence."
    )
    st.subheader("📌 Indicateurs de la période")

    vals = [
        ("Activités", len(activities)),
        ("Compléments de communication — registre unique", len(communication_unifiee)),
        ("Publications scientifiques & Documentation IST — registre unique", len(publications_ist)),
        ("Consultations", len(consultations)),
        ("Prêts", len(loans)),
        ("🧾 Fiches techniques", len(fiches_techniques)),
        ("📘 Mémoires", len(memoires)),
        ("🎓 Thèses", len(theses)),
        ("📑 Rapports scientifiques", len(rapports_scientifiques)),
        ("📰 Publications scientifiques & Documentation IST", len(publications_ist)),
        ("🗒️ Notes politiques", len(publications[publications["type_publication"] == "Note politique"]) if not publications.empty and "type_publication" in publications.columns else 0),
        ("📋 Notes de synthèse", len(publications[publications["type_publication"] == "Note de synthèse"]) if not publications.empty and "type_publication" in publications.columns else 0),
        ("Résultats scientifiques — total", len(scientific) + len(publications_ist) + len(publications[publications["type_publication"].isin(["Note politique", "Note de synthèse"])] if not publications.empty and "type_publication" in publications.columns else pd.DataFrame())),
        ("Visiteurs", len(visits)),
        ("Valorisations", len(research_data)),
        ("Profils chercheurs", len(profiles)),
    ]

    cols = st.columns(4)
    for i, (label, val) in enumerate(vals):
        with cols[i % 4]:
            st.metric(label, val)


    # ============================================================
    # RAPPORT PREMIUM COMPLET
    # ============================================================
    report_data = {
        "Activités": activities,
        "Communication": communications,
        "Médias": media_df,
        "Audiovisuel": av,
        "Formats": formats,
        "Compléments de communication": communication_unifiee,
        "Documents IST": documents,
        "Publications & Documentation IST": publications_ist,
        "Bibliothèque": library,
        "Consultations": consultations,
        "Prêts": loans,
        "Personnes": people,
        "Résultats scientifiques": scientific,
        "Fiches techniques": fiches_techniques,
        "Mémoires": memoires,
        "Thèses": theses,
        "Rapports scientifiques": rapports_scientifiques,
        "Résultats scientifiques — autres types": scientific_autres,
        "Publications scientifiques": publications,
        "Résultats scientifiques — regroupés": pd.concat([
            frame.assign(Origine=label) for label, frame in [
                ("Fiches techniques", fiches_techniques), ("Mémoires", memoires),
                ("Thèses", theses), ("Rapports scientifiques", rapports_scientifiques),
                ("Autres résultats scientifiques", scientific_autres),
                ("Publications scientifiques", publications_sans_notes),
                ("Documentation IST", documents),
                ("Notes politiques", publications[publications["type_publication"] == "Note politique"] if not publications.empty and "type_publication" in publications.columns else pd.DataFrame()),
                ("Notes de synthèse", publications[publications["type_publication"] == "Note de synthèse"] if not publications.empty and "type_publication" in publications.columns else pd.DataFrame()),
            ]
        ], ignore_index=True, sort=False),
        "Notes politiques": publications[publications["type_publication"] == "Note politique"].copy() if not publications.empty and "type_publication" in publications.columns else pd.DataFrame(columns=publications.columns),
        "Notes de synthèse": publications[publications["type_publication"] == "Note de synthèse"].copy() if not publications.empty and "type_publication" in publications.columns else pd.DataFrame(columns=publications.columns),
        "Visiteurs": visits,
        "Profils chercheurs": profiles,
        "Valorisation chercheurs": research_data,
    }

    full_pdf = complete_report_pdf(
        report_data,
        start,
        end,
        report_profile={
            "nom": "Ndeye Fota Gueye",
            "fonction": "Chargée de la communication et documentation de CRA/ISRA Saint-Louis",
        },
    )

    st.markdown("### ✨ Rapport premium")
    if full_pdf is not None:
        st.download_button(
            "✨ Télécharger le RAPPORT PREMIUM COMPLET — A4",
            full_pdf,
            "rapport_premium_ist_communication_CRA_ISRA.pdf",
            "application/pdf",
            type="primary",
            use_container_width=True,
        )
    else:
        st.error("Le module ReportLab n'est pas disponible : installez reportlab.")

    # Le classeur reprend l'architecture unifiée : les registres historiques restent
    # disponibles dans report_data pour les calculs et le PDF, mais ne sont pas dupliqués
    # en feuilles séparées lorsque leur contenu est déjà rassemblé dans un registre unique.
    _legacy_separate_sheets = {
        "Communication", "Médias", "Audiovisuel", "Formats",
        "Publications scientifiques", "Documents IST",
        "Résultats scientifiques", "Résultats scientifiques — regroupés",
    }
    # Le classeur expose les sept rubriques métier individuellement, sans feuilles
    # agrégées qui répètent les mêmes lignes et faussent la lecture des effectifs.
    # La feuille « Résultats scientifiques — autres types » reste présente pour
    # préserver tout enregistrement dont le type est inhabituel ou ancien.
    excel_report_data = {name: frame for name, frame in report_data.items() if name not in _legacy_separate_sheets}
    st.download_button(
        "⬇️ Télécharger le rapport Excel complet",
        excel_bytes(excel_report_data),
        "rapport_premium_ist_communication_CRA_ISRA.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True,
    )

    summary = pd.DataFrame({
        "Espace": [x[0] for x in vals],
        "Nombre": [x[1] for x in vals],
        "Période": [f"{start} → {end}"] * len(vals),
    })
    export_buttons(
        summary,
        "synthese_premium_ist_communication",
        "Synthèse premium — IST et Communication",
    )

    # ============================================================
    # APERÇU À L'ÉCRAN — même ordre que le PDF
    # ============================================================
    st.markdown("### 👁️ Aperçu du rapport")

    with st.expander("📣 02 — Communication — activités et Compléments de communication", expanded=True):
        st.markdown("**🗓️ Activités enregistrées — centre de Communication**")
        st.dataframe(
            activities,
            use_container_width=True,
            hide_index=True,
        )
        st.markdown("**📚 Compléments de communication — registre unique (actions, médias, audiovisuel et formats)**")
        st.dataframe(report_data["Compléments de communication"], use_container_width=True, hide_index=True)

    with st.expander("📚 03 — Documentations"):
        st.markdown("**📚 Biblio**")
        st.dataframe(library, use_container_width=True, hide_index=True)
        st.markdown("**📖 Consultations**")
        st.dataframe(consultations, use_container_width=True, hide_index=True)
        st.markdown("**👥 Personnes**")
        st.dataframe(people, use_container_width=True, hide_index=True)
        st.markdown("**📕 Prêts & retours**")
        st.dataframe(loans, use_container_width=True, hide_index=True)

    with st.expander("🔬 04 — Résultats scientifiques", expanded=False):
        scientific_preview_sections = [
            ("🧾 Fiches techniques", "Fiches techniques"),
            ("📘 Mémoires", "Mémoires"),
            ("🎓 Thèses", "Thèses"),
            ("📑 Rapports", "Rapports scientifiques"),
            ("📰 Publications scientifiques & Documentation IST", "Publications & Documentation IST"),
            ("🗒️ Notes politiques", "Notes politiques"),
            ("📋 Notes de synthèse", "Notes de synthèse"),
        ]
        for preview_title, data_key in scientific_preview_sections:
            st.markdown(f"**{preview_title}**")
            st.dataframe(report_data.get(data_key, pd.DataFrame()), use_container_width=True, hide_index=True)
        with st.expander("📊 Registre complet — toutes les catégories scientifiques", expanded=False):
            st.dataframe(report_data["Résultats scientifiques — regroupés"], use_container_width=True, hide_index=True)
        if not report_data.get("Résultats scientifiques — autres types", pd.DataFrame()).empty:
            st.markdown("**Autres types de résultats scientifiques (conservés pour éviter toute perte)**")
            st.dataframe(report_data["Résultats scientifiques — autres types"], use_container_width=True, hide_index=True)

    with st.expander("👥 05 — Visiteurs"):
        st.dataframe(
            visits,
            use_container_width=True,
            hide_index=True,
        )

    with st.expander("🧬 06 — Valorisation chercheurs"):
        st.dataframe(
            profiles,
            use_container_width=True,
            hide_index=True,
        )
        st.dataframe(
            research_data,
            use_container_width=True,
            hide_index=True,
        )


# -----------------------------
# ROUTAGE PRINCIPAL
# -----------------------------
if page == "🏠 Tableau de bord":
    render_dashboard()
elif page == "📣 Communication":
    render_communication()
    render_record_manager(page)
elif page == "📚 Documentations":
    render_documentations()
    render_record_manager(page)
elif page == "🔬 Résultats scientifiques":
    render_scientific_results()
    render_record_manager(page)
elif page == "👥 Visiteurs":
    render_visitors()
    render_record_manager(page)
elif page == "🧬 Valorisation chercheurs":
    render_researchers()
    render_record_manager(page)
elif page == "👤 Profil":
    render_profile()
elif page == "📊 Rapports & exports":
    render_reports()

st.caption("ISRA • CRA — IST et Communication | Organisation : Communication • Documentations (Biblio, Consultations, Personnes, Prêts) • Résultats scientifiques (documents, publications, notes et Documentation IST) | Formats : PDF • Excel • CSV")
