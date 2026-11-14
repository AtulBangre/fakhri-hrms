# Fakhri HRMS - Enterprise Multi-Tenant SaaS Transformation Plan
## Architecture, White-Label Branding, Security Guardrails, and Control Center

---

## 1. Executive Summary & Core Objectives

The goal of this plan is to transform **Fakhri HRMS** into an enterprise-grade, high-performance **B2B Multi-Tenant SaaS Platform**. The platform will allow the Platform Owner to create, manage, bill, and monitor hundreds of client organizations (tenants), while each client operates in a 100% isolated, white-labeled portal with their own corporate identity, legal data, and biometric face collections.

### Key Pillars
1. **100% Physical Data Isolation**: Complete separation of employee databases, biometric face vectors, payroll files, and letter documents across tenants. Zero risk of cross-tenant data leakage.
2. **Autonomous White-Label Corporate Branding**:
   - Visual Identity: Custom Logo, Favicon, Brand Color Theme (`--brand-primary`, `--brand-secondary`), and PWA Web Manifest.
   - Legal Identity: Company Legal Name, CIN, GSTIN, PAN, TAN, EPFO/ESIC Codes, and Registered Office Address.
   - Document Artifacts: Payslips, Offer Letters, Experience Certificates, and Form 16 generated with client letterheads, authorized signatures, and official stamps.
   - Communications: WhatsApp and Email alerts dispatched with tenant branding and custom signatures.
3. **Dual Routing Architecture**: Instant setup via Path Slugs (`/t/{slug}/`) plus Enterprise Subdomains (`{slug}.hrms.domain.com`) with Caddy On-Demand TLS (zero DNS token hassle).
4. **Resilient Control Plane & Resource Governance**:
   - Centralized `master.db` notification queue (prevents thread/CPU exhaustion).
   - LRU Tenant Connection Pool (prevents memory and file-descriptor leaks).
   - Tenant-bound cryptographic session cookies (prevents cross-tenant account hijacking).
5. **Zero Infrastructure Cost**: Fully optimized to run inside Oracle Cloud's Always Free Tier (4 OCPU / 24 GB RAM Ampere A1), effortlessly handling 1,000+ organizations and 50,000+ employees.

---

## 2. Multi-Tenancy Architecture: Database-Per-Tenant Model

For SQLite in Python/FastAPI, the **Database-per-Tenant** architecture guarantees extreme performance and mathematical security.

```
fakhri-hrms/
├── data/
│   ├── master.db                    # SaaS Master Control Plane (Tenants, Plans, Subscriptions, Global Queue)
│   └── tenants/                     # Isolated Tenant Storage Root
│       ├── acme_corp/               # Tenant 1: Acme Corporation
│       │   ├── hrms.db              # Isolated SQLite database (WAL mode, MMAP 256MB, full schema)
│       │   └── uploads/             # Tenant-specific file storage
│       │       ├── branding/        # logo.png, favicon.ico, stamp.png, signature.png
│       │       ├── avatars/         # Employee profile pictures
│       │       ├── punches/         # Attendance verification photos
│       │       └── letters/         # PDF letter documents
│       ├── tata_logistics/          # Tenant 2: Tata Logistics
│       │   ├── hrms.db
│       │   └── uploads/
│       └── ...                      # Tenant N
```

### Architectural Comparison
| Metric | Database-per-Tenant | Shared DB with `tenant_id` |
| :--- | :--- | :--- |
| **Data Leakage Risk** | **0% (Physical file isolation)** | High (One missed `WHERE tenant_id` leaks data) |
| **Write Concurrency** | **Infinite Horizontal Scaling** (100 DB files write simultaneously with zero lock contention) | Single database write-lock bottleneck during rush hours |
| **Client Data Export** | **Instant** (`data/tenants/slug/hrms.db`) | Complex multi-table filtered export script |
| **Offboarding / Purge** | **Atomic** (`rm -rf data/tenants/slug`) | Cascade deletion risks leaving orphaned records |
| **Enterprise Audits** | **Passes corporate compliance (GDPR/SOC2)** | Often rejected by bank/enterprise legal teams |

---

## 3. Master Platform Database Schema (`data/master.db`)

The SaaS control plane is managed by `master.db`, accessed only by the Platform Owner and system middleware.

```sql
-- 1. Tenants Registry
CREATE TABLE IF NOT EXISTS tenants (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                 TEXT NOT NULL UNIQUE,          -- Subdomain/Slug e.g. "acme"
    custom_domain        TEXT UNIQUE,                   -- e.g. "hrms.acmecorp.com"
    company_name         TEXT NOT NULL,
    admin_name           TEXT NOT NULL,
    admin_email          TEXT NOT NULL UNIQUE,
    admin_phone          TEXT,
    status               TEXT NOT NULL DEFAULT 'active', -- 'trial', 'active', 'suspended', 'expired'
    plan_id              INTEGER NOT NULL REFERENCES plans(id),
    trial_ends_at        TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tenants_slug ON tenants(slug);
CREATE INDEX IF NOT EXISTS idx_tenants_domain ON tenants(custom_domain);

-- 2. Tenant Visual Branding
CREATE TABLE IF NOT EXISTS tenant_branding (
    tenant_id            INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    portal_title         TEXT NOT NULL,                 -- Browser tab title e.g. "Acme Corp HR Portal"
    logo_filename        TEXT,                          -- Relative to tenant uploads/branding/
    favicon_filename     TEXT,
    primary_color        TEXT NOT NULL DEFAULT '#2563eb', -- Main button, active pills, headers
    secondary_color      TEXT NOT NULL DEFAULT '#1e40af', -- Hover states
    accent_color         TEXT NOT NULL DEFAULT '#3b82f6',
    sidebar_theme        TEXT NOT NULL DEFAULT 'light',   -- 'light', 'dark', 'brand'
    font_family          TEXT NOT NULL DEFAULT 'Inter'
);

-- 3. Tenant Corporate & Legal Profile (Statutory & Document Branding)
CREATE TABLE IF NOT EXISTS tenant_corporate_profile (
    tenant_id            INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    legal_entity_name    TEXT NOT NULL,                 -- e.g. "Acme Technologies Private Limited"
    cin_number           TEXT,
    gstin                TEXT,
    pan                  TEXT,
    tan                  TEXT,
    epfo_code            TEXT,
    esic_code            TEXT,
    registered_address   TEXT,
    city                 TEXT,
    state                TEXT,
    pincode              TEXT,
    signatory_name       TEXT,                          -- Authorized signatory name for letters
    signatory_title      TEXT,                          -- e.g. "Head of People & Operations"
    signature_image      TEXT,                          -- Digital signature transparent PNG
    company_seal_image   TEXT                           -- Official company stamp transparent PNG
);

-- 4. Subscription Plans
CREATE TABLE IF NOT EXISTS plans (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    code                 TEXT NOT NULL UNIQUE,          -- 'starter', 'growth', 'enterprise'
    name                 TEXT NOT NULL,
    monthly_price_inr    INTEGER NOT NULL DEFAULT 0,
    yearly_price_inr     INTEGER NOT NULL DEFAULT 0,
    max_employees        INTEGER NOT NULL DEFAULT 25,
    max_kiosks           INTEGER NOT NULL DEFAULT 1,
    max_storage_mb       INTEGER NOT NULL DEFAULT 2048, -- 2 GB
    feature_payroll      INTEGER NOT NULL DEFAULT 1,
    feature_whatsapp     INTEGER NOT NULL DEFAULT 1,
    feature_face_kiosk   INTEGER NOT NULL DEFAULT 1,
    feature_custom_domain INTEGER NOT NULL DEFAULT 0,
    feature_advanced_shifts INTEGER NOT NULL DEFAULT 1
);

-- 5. Subscriptions Ledger
CREATE TABLE IF NOT EXISTS tenant_subscriptions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id            INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plan_id              INTEGER NOT NULL REFERENCES plans(id),
    billing_cycle        TEXT NOT NULL DEFAULT 'monthly', -- 'monthly', 'yearly'
    amount_paid          INTEGER NOT NULL DEFAULT 0,
    starts_at            TEXT NOT NULL,
    expires_at           TEXT NOT NULL,
    auto_renew           INTEGER NOT NULL DEFAULT 1,
    payment_method       TEXT,
    transaction_id       TEXT,
    invoice_number       TEXT UNIQUE,
    created_at           TEXT NOT NULL
);

-- 6. Centralized Notification Queue (Solves Background Worker Bottleneck)
CREATE TABLE IF NOT EXISTS platform_notification_queue (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id            INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    channel              TEXT NOT NULL,                 -- 'whatsapp', 'email'
    recipient            TEXT NOT NULL,                 -- Phone number or Email address
    payload_json         TEXT NOT NULL,                 -- Template context & parameters
    status               TEXT NOT NULL DEFAULT 'pending', -- 'pending', 'sent', 'failed'
    attempts             INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    created_at           TEXT NOT NULL,
    sent_at              TEXT
);
CREATE INDEX IF NOT EXISTS idx_notify_queue_pending ON platform_notification_queue(status, id);

-- 7. Platform Owner Super Admins
CREATE TABLE IF NOT EXISTS platform_admins (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    email                TEXT NOT NULL UNIQUE,
    password_hash        TEXT NOT NULL,
    salt                 TEXT NOT NULL,
    name                 TEXT NOT NULL,
    created_at           TEXT NOT NULL
);
```

---

## 4. Key Architectural Fixes & Enhancements

### Fix 1: Centralized Asynchronous Notification Queue
* **The Problem**: In single-tenant mode, `NotificationWorker` polls a single database. With 50 tenants, running 50 threads will crash the VPS CPU, while sequential polling creates disk thrashing.
* **The Solution**: 
  - When any tenant punches or approves leave, the notification record is written to `master.db.platform_notification_queue` with `tenant_id`.
  - A single master `PlatformNotificationWorker` thread polls this centralized queue.
  - The worker loads the tenant's corporate branding (company name, signature) and dispatches the alert via WAHA or SMTP.
  - Zero performance overhead, zero thread bloat!

### Fix 2: Tenant-Bound Cryptographic Sessions (Zero Hijacking)
* **The Problem**: User ID 1 exists in both Tenant A and Tenant B. A domain-wide cookie would let User 1 from Acme access Tata Motors!
* **The Solution**:
  1. Session payload strictly binds `tenant_slug`:
     ```python
     payload = {
         "user_id": user["id"],
         "role": user["role"],
         "tenant_slug": request.state.tenant_slug,
         "csrf": csrf_token,
         "exp": int(time.time()) + 86400 * 14
     }
     ```
  2. In `app/auth.py`, `current_user` strictly checks:
     ```python
     if session.tenant_slug != request.state.tenant_slug:
         # Cross-tenant access attempt detected -> instant rejection & cookie flush
         return None
     ```
  3. Cookies are scoped to the specific subdomain or path.

### Fix 3: Dynamic Multi-Collection Face API (`app/face.py`)
* **The Problem**: `FaceService` previously hardcoded `settings.face_collection`.
* **The Solution**:
  - Refactor all `FaceService` methods (`identify`, `enroll`, `delete_face`) to accept an explicit `collection: str | None = None`.
  - When omitted, it dynamically resolves to `f"faces_tenant_{request.state.tenant_slug}"`.
  - On tenant creation, `ensure_collection(f"faces_tenant_{slug}")` is invoked automatically.
  - Cross-tenant false matches are 100% mathematically impossible.

### Fix 4: Dual Routing & Caddy On-Demand TLS
* **The Problem**: Let's Encrypt does not issue Wildcard SSL certificates (`*.domain.com`) via standard HTTP-01 challenges. Requiring Cloudflare API keys makes setup fragile.
* **The Solution**:
  1. **Dual Routing Support**:
     - Mode A: Subdomains (`acme.hrms.fakhriitservices.com`)
     - Mode B: Path Slugs (`hrms.fakhriitservices.com/t/acme/`) -> works immediately with zero DNS configuration!
  2. **Caddy On-Demand TLS**:
     - Caddy issues SSL certificates on-the-fly for any valid subdomain or custom domain by asking the backend:
     ```caddyfile
     {
         on_demand_tls {
             ask http://localhost:8100/api/internal/caddy-check-domain
         }
     }
     https:// {
         tls {
             on_demand
         }
         reverse_proxy api:8100
     }
     ```
     - Endpoint `/api/internal/caddy-check-domain?domain=acme.hrms...` returns HTTP 200 if the tenant exists in `master.db`.

### Fix 5: LRU Connection Pool & Resource Protection
* **The Problem**: Keeping 100+ SQLite files open indefinitely consumes file descriptors and memory buffers.
* **The Solution**:
  `TenantStoragePool` implements an LRU (Least Recently Used) cache with a capacity of 32 concurrent active tenants. Inactive tenants are automatically evicted and their file handles closed after 20 minutes of silence.

---

## 5. White-Label Branding & Internal Corporate Customization

Each tenant must experience a completely branded, private HR portal.

```
                    ┌──────────────────────────────────────────────┐
                    │           TENANT WHITE-LABEL STACK           │
                    └──────────────────────────────────────────────┘
                                           │
         ┌─────────────────────────────────┼─────────────────────────────────┐
         ▼                                 ▼                                 ▼
[ Visual Identity ]             [ Statutory & Legal ]            [ Communications ]
• Custom Logo & Favicon         • Legal Name & Reg Address       • Tenant-signed WhatsApp
• Primary & Secondary Colors    • GSTIN, PAN, TAN, CIN           • Custom Email Sender
• Dynamic CSS Variables         • EPFO & ESIC Codes              • Branded Alerts
• Custom PWA App Manifest       • Digital Signature & Stamp      • Kiosk Screen Branding
```

### 1. Dynamic UI Theme Engine
In `app/templates/base.html`, the system renders dynamic CSS variables:
```html
<style>
  :root {
    --brand-primary: {{ branding.primary_color or '#2563eb' }};
    --brand-primary-hover: {{ branding.secondary_color or '#1d4ed8' }};
    --brand-accent: {{ branding.accent_color or '#3b82f6' }};
  }
</style>
```
* **Sidebar & Header**: Shows the client's custom logo.
* **Kiosk Terminal**: Gate display shows `"Welcome to [Company Name]"` with their official logo.
* **PWA Web Manifest**: `/manifest.webmanifest` and `/kiosk.webmanifest` dynamically generate icons and tenant name so homescreen shortcuts appear like native bespoke company apps.

### 2. Document & Statutory Artifact Engine
PDF documents dynamically pull legal data from `tenant_corporate_profile`:
* **Payslips (`app/services/payroll.py`)**:
  - Header: Tenant Legal Name, Registered Address, CIN, and GSTIN.
  - Tax/Statutory details: Tenant PAN, TAN, EPFO, and ESIC numbers.
  - Sign-off: Tenant Authorized Signatory Name, Designation, Digital Signature PNG, and Company Stamp PNG.
* **Official Letters (`app/routes/letters.py`)**:
  - Offer Letters, Appointment Letters, Relieving Certificates, and Experience Letters automatically render with the tenant's official letterhead, logo, and seal.
* **Tax Documents & Form 16 (`app/routes/tax.py`)**:
  - Pre-fills employer tax identification from the tenant's corporate profile.

---

## 6. Platform Owner Control Center (Super Admin Portal)

Accessible at `owner.hrms.fakhriitservices.com` or `/super-admin`.

### Dashboard Capabilities
1. **Commercial & Operational Overview**:
   - Total Active Organizations, Total Enrolled Employees, Total Gate Kiosks.
   - Monthly Recurring Revenue (MRR) & Annual Recurring Revenue (ARR).
   - Near-expiry trial alerts (automated renewal follow-up).
2. **1-Click Tenant Provisioner (<800ms automated onboarding)**:
   - Form Inputs: Company Name, Slug, Plan, Admin Email, Admin Phone.
   - Automation Routine:
     1. Creates folder structure: `data/tenants/{slug}/uploads/{branding,avatars,punches,letters}`.
     2. Instantiates `hrms.db` and applies full schema with standard Indian leave types, shifts, and default settings.
     3. Creates HR Admin account with hashed scrypt credentials.
     4. Provisions Face API collection `faces_tenant_{slug}`.
     5. Saves branding colors and corporate profile.
     6. Sends Welcome Email to client HR with private portal login credentials.
3. **Tenant Lifecycle Controls**:
   - **Instant Suspend / Reactivate**: Toggles access; suspended tenants see a friendly "Subscription Paused" page.
   - **Plan Upgrades / Quota Controls**:
     - Employee limit check: blocks adding staff beyond plan quota.
     - Kiosk limit check: blocks registering unauthorized gate devices.
   - **1-Click Tenant Database Export**: Download raw `.db` snapshot for offboarding or disaster recovery.

---

## 7. Phased Implementation Roadmap

```
Phase 1: Master Control Plane & Tenant Pool (Foundation)
Phase 2: Tenant Resolution Middleware & Dynamic Branding
Phase 3: Centralized Notification Queue & Auth Hardening
Phase 4: Face API Multi-Collection & Branded Document Engine
Phase 5: Platform Owner UI, Caddy On-Demand TLS & Production Verification
```

| Phase | Core Deliverables | Timeframe |
| :--- | :--- | :--- |
| **Phase 1: Foundation** | • Create `app/master_db.py` and initialize `data/master.db`.<br>• Refactor `app/db.py` to support dynamic `db_path`.<br>• Implement `TenantStoragePool` with LRU connection eviction.<br>• Move existing demo database to `data/tenants/default/hrms.db`. | 2 Days |
| **Phase 2: Routing & UI** | • Build `TenantResolutionMiddleware` (Subdomain + Path Slug support).<br>• Update `app/deps.py` (`get_storage`, `get_settings_dep`).<br>• Inject dynamic CSS variables and tenant logos into `base.html` and `kiosk.html`.<br>• Implement tenant-isolated uploads in `app/uploads.py`. | 2 Days |
| **Phase 3: Queue & Security** | • Implement `platform_notification_queue` in `master.db`.<br>• Refactor `NotificationWorker` to process global queue with tenant branding.<br>• Update `app/auth.py` with `tenant_slug` bound session cookies. | 1.5 Days |
| **Phase 4: Biometrics & Docs** | • Refactor `FaceService` to dynamically use `faces_tenant_{slug}`.<br>• Bind tenant corporate profile (GST, PAN, Sign, Stamp) to Payslip and Letter PDFs.<br>• Customize WhatsApp / Email notification signatures. | 1.5 Days |
| **Phase 5: Owner Portal & Deploy** | • Build Platform Owner Dashboard (`/super-admin`) with 1-click tenant provisioner.<br>• Implement Caddy On-Demand TLS check endpoint (`/api/internal/caddy-check-domain`).<br>• Configure `deploy/Caddyfile` and run full regression test suite. | 2 Days |

---

## 8. Summary of Benefits

1. **Enterprise Product Readiness**: Directly sellable to corporate clients with full white-label pride.
2. **Mathematical Data Safety**: Physical database separation guarantees zero risk of data leakage.
3. **Zero Hosting Cost Inflation**: Runs entirely within Oracle Cloud Always Free Tier (4 OCPU / 24 GB RAM) for up to 1,000+ organizations.
4. **Preserves Existing Code**: 95% of existing business logic remains intact through FastAPI's dependency injection (`storage = Depends(get_storage)`).
