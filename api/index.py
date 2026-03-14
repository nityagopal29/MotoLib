from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
import mysql.connector
from mysql.connector import pooling
from contextlib import asynccontextmanager
from typing import Optional
import traceback
import os
from pathlib import Path
from dotenv import load_dotenv
import random

# Load environment variables - use explicit path so uvicorn subprocess can find it
load_dotenv(dotenv_path=Path(__file__).parent / '.env')

# ═══════════════════════════════════════════════════════════════
# ENVIRONMENT CONFIGURATION
# ═══════════════════════════════════════════════════════════════
ENV = os.getenv("ENVIRONMENT", "development") # development or production
DEBUG = ENV == "development"

# Database Configuration
DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME", "motolib"),
    "charset": "utf8mb4",
    "collation": "utf8mb4_unicode_ci",
    "autocommit": True,
    "pool_name": "motolib_pool",
    "pool_size": int(os.getenv("DB_POOL_SIZE", "5")),
    "pool_reset_session": True,
    "ssl_disabled": False # Required for TiDB Serverless
}

# ═══════════════════════════════════════════════════════════════
# DATABASE CONNECTION POOL
# ═══════════════════════════════════════════════════════════════
connection_pool = None

def init_db_pool():
    """Initialize database connection pool"""
    global connection_pool
    try:
        # Verify all required config is present
        required_keys = ["host", "user", "password", "database"]
        missing = [k for k in required_keys if not DB_CONFIG.get(k)]
        if missing:
            raise ValueError(f"Missing database configuration: {', '.join(missing)}")

        connection_pool = pooling.MySQLConnectionPool(**DB_CONFIG)
        print("✅ Database connection pool initialized")
        return True
    except Exception as e:
        print(f"❌ Failed to initialize database pool: {e}")
        traceback.print_exc() # Show full error for diagnosis
        return False

def get_db_connection():
    """Get database connection from pool with lazy initialization for Vercel"""
    global connection_pool
    try:
        if connection_pool is None:
            # Lazy initialization for Serverless environments like Vercel
            if not init_db_pool():
                raise HTTPException(status_code=503, detail="Failed to initialize database pool")

        conn = connection_pool.get_connection()
        return conn
    except mysql.connector.Error as err:
        print(f"❌ Database connection error: {err}")
        raise HTTPException(status_code=503, detail="Database connection failed")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle events for the FastAPI application"""
    print("\n" + "="*60)
    print("MOTOLIB SERVER STARTING")
    print("="*60)
    print(f"Environment: {ENV}")
    print(f"Debug Mode: {DEBUG}")

    # Initialize database pool
    if init_db_pool():
        # Test connection
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM companies")
            result = cursor.fetchone()
            count = result[0] if result else 0
            cursor.close()
            conn.close()
            print(f"Database connected ({count} companies found)")
        except Exception as e:
            print(f"Startup database test failed: {e}")
    else:
        print("Failed to initialize database pool on startup")

    print("="*60 + "\n")

    yield

    print("\n🛑 Shutting down MotoLib server...")

# Initialize FastAPI
app = FastAPI(
    title="MotoLib",
    description="The Ultimate Motorcycle Encyclopedia",
    version="1.0.0",
    docs_url="/api/docs" if DEBUG else None,
    redoc_url="/api/redoc" if DEBUG else None,
    lifespan=lifespan
)

# ═══════════════════════════════════════════════════════════════
# CORS CONFIGURATION (for API access)
# ═══════════════════════════════════════════════════════════════
allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════════════════════════
# STATIC FILES & TEMPLATES
# ═══════════════════════════════════════════════════════════════
# Use absolute paths for Vercel compatibility
BASE_DIR = Path(__file__).resolve().parent

# Check if static folder exists to prevent crashes if forgotten in git
if (BASE_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ═══════════════════════════════════════════════════════════════
# HEALTH CHECK ENDPOINT
# ═══════════════════════════════════════════════════════════════════
@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone() # Consume result
        cursor.close()
        conn.close()

        return {
            "status": "healthy",
            "environment": ENV,
            "database": "connected"
        }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "error": str(e)
            }
        )

# ═══════════════════════════════════════════════════════════════
# PAGE ROUTES
# ═══════════════════════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request):
    """Landing page"""
    try:
        return templates.TemplateResponse("landing.html", {"request": request})
    except Exception as e:
        if DEBUG:
            raise
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/companies", response_class=HTMLResponse)
async def companies(request: Request):
    """Display all companies"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT * FROM companies ORDER BY name")
        companies = cursor.fetchall()

        cursor.close()
        conn.close()

        return templates.TemplateResponse("companies.html", {
            "request": request,
            "companies": companies
        })
    except Exception as e:
        if DEBUG:
            print(f"❌ Error in companies route: {e}")
            traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/categories/{company_id}", response_class=HTMLResponse)
async def categories(request: Request, company_id: int):
    """Display categories for a specific company"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Get company info
        cursor.execute("SELECT * FROM companies WHERE id = %s", (company_id,))
        company = cursor.fetchone()

        if not company:
            cursor.close()
            conn.close()
            raise HTTPException(status_code=404, detail="Company not found")

        # Get categories with bikes from this company
        cursor.execute("""
        SELECT DISTINCT c.id, c.name, c.image, c.description
        FROM categories c
        JOIN bikes b ON c.id = b.category_id
        WHERE b.company_id = %s
        ORDER BY c.name
        """, (company_id,))
        categories = cursor.fetchall()

        cursor.close()
        conn.close()

        return templates.TemplateResponse("categories.html", {
            "request": request,
            "company": company,
            "company_id": company_id, # Pass for template links
            "categories": categories
        })
    except HTTPException:
        raise
    except Exception as e:
        if DEBUG:
            print(f"❌ Error in categories route: {e}")
            traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/bikes", response_class=HTMLResponse)
async def bikes_list(
    request: Request, 
    company_id: int = Query(..., description="Company ID"), 
    category_id: int = Query(..., description="Category ID")
):
    """Display bikes for a specific company and category"""
    print(f"DEBUG: Handling /bikes request - company_id: {company_id}, category_id: {category_id}")
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Get bikes
        cursor.execute("""
        SELECT b.*, c.name as company_name, cat.name as category_name
        FROM bikes b
        JOIN companies c ON b.company_id = c.id
        JOIN categories cat ON b.category_id = cat.id
        WHERE b.company_id = %s AND b.category_id = %s
        ORDER BY b.name
        """, (company_id, category_id))
        bikes = cursor.fetchall()

        cursor.close()
        conn.close()

        return templates.TemplateResponse("bikes.html", {
            "request": request,
            "bikes": bikes,
            "company_id": company_id,
            "category_id": category_id
        })
    except Exception as e:
        if DEBUG:
            print(f"❌ Error in bikes route: {e}")
            traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/bike/{bike_id}", response_class=HTMLResponse)
async def bike_detail(request: Request, bike_id: int):
    """Display detailed information for a single bike"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Get bike details
        cursor.execute("""
        SELECT 
        b.id,
        b.name as bike_name,
        b.description,
        b.year,
        b.image_url,
        b.website_link,
        b.company_id,
        b.category_id,
        c.name as company_name,
        cat.name as category_name
        FROM bikes b
        JOIN companies c ON b.company_id = c.id
        JOIN categories cat ON b.category_id = cat.id
        WHERE b.id = %s
        """, (bike_id,))
        bike = cursor.fetchone()

        if not bike:
            cursor.close()
            conn.close()
            raise HTTPException(status_code=404, detail="Bike not found")

        # Get prev/next bikes in same company + category
        cursor.execute("""
        SELECT id FROM bikes 
        WHERE company_id = %s AND category_id = %s
        ORDER BY name
        """, (bike['company_id'], bike['category_id']))

        all_bike_ids = [row['id'] for row in cursor.fetchall()]

        cursor.close()
        conn.close()

        # Calculate prev/next with loop
        current_index = all_bike_ids.index(bike_id)
        total_bikes = len(all_bike_ids)

        prev_bike_id = all_bike_ids[(current_index - 1) % total_bikes]
        next_bike_id = all_bike_ids[(current_index + 1) % total_bikes]

        return templates.TemplateResponse("bike_detail.html", {
            "request": request,
            "bike": bike,
            "prev_bike_id": prev_bike_id,
            "next_bike_id": next_bike_id
        })
    except HTTPException:
        raise
    except Exception as e:
        if DEBUG:
            print(f"❌ Error in bike_detail route: {e}")
            traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")

# ═══════════════════════════════════════════════════════════════
# API ENDPOINTS (for navbar functionality)
# ═══════════════════════════════════════════════════════════════
@app.get("/api/browse-data")
async def browse_data():
    """Get all brands and bikes for Browse dropdown"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Get all brands
        cursor.execute("SELECT id, name FROM companies ORDER BY name")
        brands = cursor.fetchall()

        # Get all bikes with company names
        cursor.execute("""
        SELECT b.id, b.name, c.name as company_name
        FROM bikes b
        JOIN companies c ON b.company_id = c.id
        ORDER BY c.name, b.name
        """)
        bikes = cursor.fetchall()

        cursor.close()
        conn.close()

        return {"brands": brands, "bikes": bikes}
    except Exception as e:
        if DEBUG:
            print(f"❌ Error in browse_data: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/filter-data")
async def filter_data():
    """Get brands and categories for Filter dropdown"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT id, name FROM companies ORDER BY name")
        brands = cursor.fetchall()

        cursor.execute("SELECT id, name FROM categories ORDER BY name")
        categories = cursor.fetchall()

        cursor.close()
        conn.close()

        return {"brands": brands, "categories": categories}
    except Exception as e:
        if DEBUG:
            print(f"❌ Error in filter_data: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/bike-mappings")
async def bike_mappings():
    """Get bike-brand-category mappings for filter logic"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
        SELECT DISTINCT company_id, category_id
        FROM bikes
        """)
        mappings = cursor.fetchall()

        cursor.close()
        conn.close()

        return mappings
    except Exception as e:
        if DEBUG:
            print(f"❌ Error in bike_mappings: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/random-bike")
async def random_bike():
    """Redirect to a random bike (Lucky Star feature)"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT id FROM bikes")
        all_bikes = cursor.fetchall()

        cursor.close()
        conn.close()

        if all_bikes:
            random_bike = random.choice(all_bikes)
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"/bike/{random_bike['id']}")
        else:
            raise HTTPException(status_code=404, detail="No bikes available")
    except HTTPException:
        raise
    except Exception as e:
        if DEBUG:
            print(f"❌ Error in random_bike: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/filter-results", response_class=HTMLResponse)
async def filter_results(
    request: Request, 
    brands: Optional[str] = Query(None, description="Brands"), 
    categories: Optional[str] = Query(None, description="Categories")
):
    """Display filtered bike results"""
    print(f"DEBUG: Handling /filter-results request - brands: {brands}, categories: {categories}")
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Build query based on filters
        query = """
        SELECT b.*, c.name as company_name, cat.name as category_name
        FROM bikes b
        JOIN companies c ON b.company_id = c.id
        JOIN categories cat ON b.category_id = cat.id
        WHERE 1=1
        """
        params = []

        if brands:
            brand_ids = [int(x) for x in brands.split(',')]
            placeholders = ','.join(['%s'] * len(brand_ids))
            query += f" AND b.company_id IN ({placeholders})"
            params.extend(brand_ids)

        if categories:
            category_ids = [int(x) for x in categories.split(',')]
            placeholders = ','.join(['%s'] * len(category_ids))
            query += f" AND b.category_id IN ({placeholders})"
            params.extend(category_ids)

        query += " ORDER BY c.name, b.name"

        cursor.execute(query, params)
        bikes = cursor.fetchall()

        cursor.close()
        conn.close()

        return templates.TemplateResponse("bikes.html", {
            "request": request,
            "bikes": bikes,
            "company_id": None,
            "category_id": None
        })
    except Exception as e:
        if DEBUG:
            print(f"❌ Error in filter_results: {e}")
            traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")

# ═══════════════════════════════════════════════════════════════
# ERROR HANDLERS
# ═══════════════════════════════════════════════════════════════
@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    """Custom 404 page"""
    return templates.TemplateResponse(
        "404.html" if (BASE_DIR / "templates" / "404.html").exists() else "landing.html",
        {"request": request},
        status_code=404
    )

@app.exception_handler(500)
async def server_error_handler(request: Request, exc: HTTPException):
    """Custom 500 page"""
    if DEBUG:
        raise exc
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"}
    )

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=DEBUG
    )
