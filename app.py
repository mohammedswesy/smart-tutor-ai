import os
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, jsonify
import mysql.connector
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "smart_tutor_2026_professional_key"

# إعدادات اختيارية
STRICT_MANDATORY_ONBOARDING = False  # ضع True لتفعيل الإلزام بإنهاء الإعداد الأولي قبل لوحة الطالب

# إعدادات المجلدات
UPLOAD_FOLDER = 'static/uploads'
SUBMISSIONS_FOLDER = 'static/submissions'
for folder in [UPLOAD_FOLDER, SUBMISSIONS_FOLDER]:
    os.makedirs(folder, exist_ok=True)

def get_db_connection():
    try:
        conn = mysql.connector.connect(
            host="localhost",
            user="root",
            password="",
            database="smart_tutors",
            charset='utf8mb4',
            collation='utf8mb4_general_ci'
        )
        return conn
    except mysql.connector.Error as err:
        print(f"Error: {err}")
        return None

def initialize_database():
    """Create necessary tables if they don't exist"""
    conn = get_db_connection()
    if not conn:
        return
    
    cursor = conn.cursor()
    try:
        # Create student_preferences table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS student_preferences (
                id INT AUTO_INCREMENT PRIMARY KEY,
                student_id INT NOT NULL,
                educational_level VARCHAR(50),
                preferred_subjects TEXT,
                preferred_times TEXT,
                max_hourly_rate DECIMAL(10,2),
                teaching_style VARCHAR(50),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
                UNIQUE KEY unique_student_prefs (student_id)
            )
        """)
        # ensure student table also supports quick preference storage
        try:
            cursor.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS preferred_subjects TEXT")
            cursor.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS preferred_times TEXT")
            cursor.execute("ALTER TABLE students MODIFY preferred_subjects TEXT NOT NULL DEFAULT ''")
            cursor.execute("ALTER TABLE students MODIFY preferred_times TEXT NOT NULL DEFAULT ''")
        except mysql.connector.Error as inner_err:
            # ignore if already present or unsupported for old versions
            if inner_err.errno not in (1060,):
                print(f"Ignore initialize DB field addition error (not fatal): {inner_err}")

        # ensure tutors has required matching fields too
        try:
            cursor.execute("ALTER TABLE tutors ADD COLUMN IF NOT EXISTS experience_level VARCHAR(50)")
            cursor.execute("ALTER TABLE tutors ADD COLUMN IF NOT EXISTS teaching_style VARCHAR(100)")
            cursor.execute("ALTER TABLE tutors ADD COLUMN IF NOT EXISTS available_time VARCHAR(255)")
            cursor.execute("ALTER TABLE tutors ADD COLUMN IF NOT EXISTS hourly_rate DECIMAL(10,2) DEFAULT 0")
            cursor.execute("ALTER TABLE tutors ADD COLUMN IF NOT EXISTS subject VARCHAR(100)")
            cursor.execute("ALTER TABLE tutors ADD COLUMN IF NOT EXISTS bio TEXT")
            cursor.execute("ALTER TABLE tutors MODIFY experience_level VARCHAR(50) NOT NULL DEFAULT ''")
            cursor.execute("ALTER TABLE tutors MODIFY teaching_style VARCHAR(100) NOT NULL DEFAULT ''")
            cursor.execute("ALTER TABLE tutors MODIFY available_time VARCHAR(255) NOT NULL DEFAULT ''")
            cursor.execute("ALTER TABLE tutors MODIFY subject VARCHAR(100) NOT NULL DEFAULT ''")
            cursor.execute("ALTER TABLE tutors MODIFY bio TEXT NOT NULL")
            cursor.execute("ALTER TABLE tutors MODIFY hourly_rate DECIMAL(10,2) NOT NULL DEFAULT 0")
        except mysql.connector.Error as inner_err:
            if inner_err.errno not in (1060,):
                print(f"Ignore initialize DB tutors field addition error (not fatal): {inner_err}")

        conn.commit()
        print("Database tables initialized successfully")
    except mysql.connector.Error as err:
        print(f"Database initialization error: {err}")
    finally:
        cursor.close()
        conn.close()

# Initialize database tables
initialize_database()

# --- Helpers ---
def is_logged_in():
    return 'user_id' in session

def get_user_role():
    return session.get('role')
def get_student_level(avg_score):
    if avg_score < 50:
        return "beginner"
    elif avg_score < 80:
        return "intermediate"
    else:
        return "advanced"


def normalize_educational_level(level):
    mapping = {
        'elementary': 'beginner',
        'middle': 'beginner',
        'secondary': 'intermediate',
        'college': 'advanced',
        'professional': 'advanced'
    }
    if not level:
        return None
    return mapping.get(level.strip().lower(), 'intermediate')


def get_tutor_score(tutor, student_level, student_avg_score, avg_hourly_rate=30, student_prefs=None):
    """
    Calculate comprehensive tutor score based on multiple factors:
    - Rating (25%): Higher-rated tutors score better
    - Experience Match (20%): Tutors with matching experience level
    - Subject Match (15%): Tutor teaches preferred subjects
    - Time Match (15%): Tutor available during preferred times
    - Budget Match (15%): Tutor rate within student's budget
    - Cost-Effectiveness (10%): Value for money compared to market
    """
    score = 0
    reasons = []
    
    # Factor 1: Rating (25%)
    rating = tutor.get('rating', 0)
    rating_score = min((rating / 5.0) * 25, 25) if rating else 0
    score += rating_score
    if rating >= 4.5:
        reasons.append("⭐ Highly rated")
    
    # Factor 2: Experience Match (20%)
    # Default to intermediate if not in database
    experience_level = tutor.get('experience_level', 'intermediate')
    level_match_score = 0
    if student_level == experience_level:
        level_match_score = 20
        reasons.append("✓ Perfect experience match")
    elif (student_level == 'beginner' and experience_level in ['beginner', 'intermediate']) or \
         (student_level == 'intermediate' and experience_level in ['intermediate', 'advanced']) or \
         (student_level == 'advanced' and experience_level == 'advanced'):
        level_match_score = 15
        reasons.append("✓ Suitable experience level")
    else:
        level_match_score = 8
        reasons.append("~ Experience level acceptable")
    score += level_match_score
    
    # Factor 3: Subject Match (15%) - based on student preferences
    subject_score = 0
    if student_prefs and student_prefs.get('preferred_subjects'):
        preferred_subjects_raw = student_prefs.get('preferred_subjects') or ''
        preferred_subjects = [s.strip().lower() for s in preferred_subjects_raw.split(',') if s.strip()]
        tutor_subject = (tutor.get('subject') or '').strip().lower()
        if tutor_subject and any(pref in tutor_subject for pref in preferred_subjects):
            subject_score = 15
            reasons.append("📚 Teaches preferred subject")
        elif tutor_subject:
            subject_score = 8
            reasons.append("📖 Related subject area")
    else:
        subject_score = 10  # Default if no preferences
    score += subject_score
    
    # Factor 4: Time Match (15%) - based on student preferences
    time_score = 0
    if student_prefs and student_prefs.get('preferred_times'):
        preferred_times_raw = student_prefs.get('preferred_times') or ''
        preferred_times = [s.strip().lower() for s in preferred_times_raw.split(',') if s.strip()]
        tutor_available_time = (tutor.get('available_time') or '').strip().lower()
        if tutor_available_time and any(pref in tutor_available_time for pref in preferred_times):
            time_score = 15
            reasons.append("⏰ Available during preferred times")
        else:
            time_score = 8
            reasons.append("🕐 Some availability overlap")
    else:
        time_score = 10  # Default if no preferences
    score += time_score
    
    # Factor 5: Budget Match (15%) - based on student preferences
    budget_score = 0
    if student_prefs and student_prefs.get('max_hourly_rate'):
        max_budget = float(student_prefs['max_hourly_rate'])
        tutor_rate = float(tutor.get('hourly_rate', avg_hourly_rate))
        if tutor_rate <= max_budget:
            budget_score = 15
            reasons.append("💰 Within budget")
        elif tutor_rate <= max_budget * 1.2:
            budget_score = 10
            reasons.append("💵 Slightly over budget")
        else:
            budget_score = 5
            reasons.append("💸 Over budget")
    else:
        budget_score = 10  # Default if no preferences
    score += budget_score

    # Factor 6: Teaching style match (5%) - encourage alignment between tutor and student preference
    style_score = 0
    tutor_style = (tutor.get('teaching_style') or '').strip().lower()
    student_style = ''
    if student_prefs and student_prefs.get('teaching_style'):
        student_style = (student_prefs.get('teaching_style') or '').strip().lower()

    if student_style and tutor_style:
        if student_style in tutor_style or tutor_style in student_style:
            style_score = 5
            reasons.append("🧭 Teaching style aligned")
        else:
            style_score = 2
            reasons.append("🧭 Teaching style somewhat different")
    else:
        style_score = 3
        reasons.append("🧭 Teaching style info not fully available")

    score += style_score

    # Factor 7: Cost-Effectiveness (10%) - compared to market average
    hourly_rate = tutor.get('hourly_rate', avg_hourly_rate)
    hourly_rate = float(hourly_rate)
    avg_hourly_rate_float = float(avg_hourly_rate)
    
    if hourly_rate <= avg_hourly_rate_float * 0.8:
        cost_score = 10
        reasons.append("💎 Great value")
    elif hourly_rate <= avg_hourly_rate_float * 1.2:
        cost_score = 7
        reasons.append("✅ Fair pricing")
    else:
        cost_score = 4
        reasons.append("💸 Premium pricing")
    score += cost_score
    
    return round(score, 1), reasons

def recommend_tutors(student_id, subject, limit=5):
    """
    Enhanced recommendation algorithm returning multiple tutors with detailed reasoning.
    Uses student preferences for personalized matching.
    Returns: list of tutors with scores and recommendations
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Get student's average score and level
        cursor.execute(
            "SELECT AVG(score) as avg FROM grades WHERE student_id=%s",
            (student_id,)
        )
        scores = cursor.fetchone()
        avg_score = float(scores['avg']) if scores['avg'] else 50
        level = get_student_level(avg_score)
        
        # Get student's preferences
        cursor.execute("SELECT * FROM student_preferences WHERE student_id=%s", (student_id,))
        student_prefs = cursor.fetchone()

        # Override level based on onboarding educational level if provided
        if student_prefs and student_prefs.get('educational_level'):
            normalized = normalize_educational_level(student_prefs.get('educational_level'))
            if normalized:
                level = normalized
        
        # Get tutors for the subject (include available_time)
        cursor.execute("""
            SELECT 
                id, name, subject, bio, hourly_rate, rating, 
                profile_pic, email, available_time, experience_level, teaching_style
            FROM tutors 
            WHERE subject LIKE %s
            ORDER BY rating DESC
            LIMIT 20
        """, (f"%{subject}%",))
        
        tutors = cursor.fetchall()
        
        if not tutors:
            return [], level, avg_score
        
        # Add defaults for optional fields and avoid None values for string fields
        for tutor in tutors:
            tutor['subject'] = (tutor.get('subject') or '').strip()
            tutor['available_time'] = (tutor.get('available_time') or '').strip()
            tutor['experience_level'] = (tutor.get('experience_level') or 'intermediate').strip()
            tutor['teaching_style'] = (tutor.get('teaching_style') or '').strip()
            tutor['bio'] = (tutor.get('bio') or '').strip()
            tutor['hourly_rate'] = float(tutor.get('hourly_rate') or 0)

            if 'available_slots' not in tutor:
                tutor['available_slots'] = 5
            if 'avg_rating' not in tutor:
                tutor['avg_rating'] = tutor.get('rating', 0)
        
        # Calculate average hourly rate for cost-effectiveness comparison
        avg_hourly_rate = 30.0  # Default if no tutors
        if tutors:
            rates = []
            for t in tutors:
                rate = t.get('hourly_rate', 30)
                # Convert Decimal to float if needed
                if hasattr(rate, 'real'):  # Check if it's a number-like object
                    rate = float(rate)
                rates.append(rate)
            # Ensure all rates are float before dividing
            total = sum(float(r) for r in rates)
            avg_hourly_rate = total / len(rates)
        
        # Score and rank all tutors
        scored_tutors = []
        for tutor in tutors:
            score, reasons = get_tutor_score(tutor, level, avg_score, avg_hourly_rate, student_prefs)
            scored_tutors.append({
                'tutor': tutor,
                'score': score,
                'reasons': reasons,
                'confidence': min(100, int(score * 2))
            })
        
        # Sort by score descending
        scored_tutors.sort(key=lambda x: x['score'], reverse=True)
        
        return scored_tutors[:limit], level, avg_score
    finally:
        cursor.close()
        conn.close()

def recommend_tutor(student_id, subject):
    """Legacy function - returns top recommendation"""
    recommendations, level, avg_score = recommend_tutors(student_id, subject, limit=1)
    if recommendations:
        return recommendations[0]['tutor'], level, avg_score
    return None, level, avg_score

@app.route('/display/<filename>')
def display_image(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/')
def home():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT id, name, subject, profile_pic FROM tutors ORDER BY id DESC")
        tutors = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    return render_template("index.html", tutors=tutors)

# -------------------------
# التسجيل (Student / Tutor)
# -------------------------

# @app.route('/register_student', methods=['GET', 'POST'])
# def register_student():
#     if request.method == 'POST':
#         name = request.form.get('name')
#         email = request.form.get('email')
#         password = request.form.get('password')

#         conn = get_db_connection()
#         cursor = conn.cursor()
#         try:
#             # 1) users
#             cursor.execute(
#                 "INSERT INTO users (email, password, role) VALUES (%s, %s, 'student')",
#                 (email, password)
#             )
#             user_id = cursor.lastrowid

#             # 2) students (بدون password)
#             cursor.execute(
#                 "INSERT INTO students (id, name, email) VALUES (%s, %s, %s)",
#                 (user_id, name, email)
#             )

#             conn.commit()
#             flash('تم التسجيل بنجاح! يمكنك الآن تسجيل الدخول.', 'success')
#             return redirect(url_for('login'))

#         except mysql.connector.Error as err:
#             conn.rollback()
#             print(f"DATABASE ERROR (Student): {err}")
#             flash(f"خطأ في قاعدة البيانات: {err}", "danger")

#         finally:
#             cursor.close()
#             conn.close()

#     return render_template("add_student.html")


# @app.route('/register_tutor', methods=['GET', 'POST'])
# def register_tutor():
#     if request.method == 'POST':
#         tutor_name = request.form.get('name')
#         tutor_email = request.form.get('email')
#         tutor_subject = request.form.get('subject')
#         tutor_password = request.form.get('password')

#         conn = get_db_connection()
#         cursor = conn.cursor()
#         try:
#             # 1) users
#             cursor.execute(
#                 "INSERT INTO users (email, password, role) VALUES (%s, %s, 'tutor')",
#                 (tutor_email, tutor_password)
#             )
#             user_id = cursor.lastrowid

#             # 2) tutors (بدون password)
#             cursor.execute(
#                 "INSERT INTO tutors (id, name, email, subject) VALUES (%s, %s, %s, %s)",
#                 (user_id, tutor_name, tutor_email, tutor_subject)
#             )

#             conn.commit()
#             flash('تم تسجيلك كمعلم بنجاح! يمكنك الآن تسجيل الدخول.', 'success')
#             return redirect(url_for('login'))

#         except mysql.connector.Error as err:
#             conn.rollback()
#             print(f"DATABASE ERROR (Tutor): {err}")
#             flash(f"خطأ في قاعدة البيانات: {err}", "danger")

#         finally:
#             cursor.close()
#             conn.close()

#     return render_template("add_tutor.html")

@app.route('/register_student', methods=['GET', 'POST'])
def register_student():
    flash("Student accounts are created by admin only.", "danger")
    return redirect(url_for('login'))


@app.route('/register_tutor', methods=['GET', 'POST'])
def register_tutor():
    flash("Tutor accounts are created by admin only.", "danger")
    return redirect(url_for('login'))

# -------------------------
# تسجيل الدخول / تسجيل الخروج
# -------------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # استعلام ذكي بيجيب بيانات الحساب وبيروح يشوف الاسم من جدول الطالب أو المعلم
        query = """
            SELECT u.*, 
                   COALESCE(s.name, t.name, 'مستخدم جديد') as display_name 
            FROM users u
            LEFT JOIN students s ON u.id = s.id
            LEFT JOIN tutors t ON u.id = t.id
            WHERE u.email = %s
        """
        cursor.execute(query, (email,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
        
        # التحقق من كلمة المرور (بيدعم التشفير أو النص العادي عشان حساباتك القديمة)
        if user and (check_password_hash(user['password'], password) or user['password'] == password):
            session['user_id'] = user['id']
            session['name'] = user['display_name'] # هيك حلينا KeyError: 'name'
            session['role'] = user['role']
            flash(f"أهلاً بك يا {user['display_name']}!", "success")

            if user['role'] == 'student' and not has_completed_onboarding(user['id']):
                flash("لم تُكمل تفضيلاتك بعد. يمكنك تحديثها الآن من لوحة الطالب.", "info")
                return redirect(url_for('dashboard_router', onboarding=1))

            if user['role'] == 'tutor' and not has_completed_tutor_profile(user['id']):
                flash("أكمل ملفك الشخصي كمعلم ليظهر للطلاب كتوصية أفضل.", "info")
                return redirect(url_for('settings'))

            return redirect(url_for('dashboard_router'))
        else:
            flash("خطأ في البريد الإلكتروني أو كلمة المرور", "danger")
            
    prompt_onboarding = request.args.get('onboarding') == '1'
    return render_template('login.html', prompt_onboarding=prompt_onboarding)

def admin_only():
    return 'user_id' in session and session.get('role') == 'admin'

def is_logged_in():
    return 'user_id' in session

def student_only():
    return is_logged_in() and session.get('role') == 'student'

def has_completed_onboarding(student_id):
    """Check if student has completed onboarding"""
    conn = get_db_connection()
    if not conn:
        return False
    
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM student_preferences WHERE student_id=%s", (student_id,))
        return cursor.fetchone() is not None
    finally:
        cursor.close()
        conn.close()


def has_completed_tutor_profile(tutor_id):
    """Check if tutor has required fields for matching."""
    conn = get_db_connection()
    if not conn:
        return False

    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT id FROM tutors WHERE id=%s "
            "AND subject IS NOT NULL AND subject<>'' "
            "AND experience_level IS NOT NULL AND experience_level<>'' "
            "AND available_time IS NOT NULL AND available_time<>'' "
            "AND hourly_rate IS NOT NULL",
            (tutor_id,)
        )
        return cursor.fetchone() is not None
    finally:
        cursor.close()
        conn.close()


def merge_csv_unique(existing_value, new_value):
    existing_items = []
    if existing_value:
        existing_items = [item.strip() for item in existing_value.split(',') if item.strip()]

    if new_value:
        new_items = [item.strip() for item in new_value.split(',') if item.strip()]
        for item in new_items:
            if item.lower() not in [e.lower() for e in existing_items]:
                existing_items.append(item)

    return ','.join(existing_items)


def tutor_only():
    return is_logged_in() and session.get('role') == 'tutor'

@app.route('/dashboard_router')
def dashboard_router():
    if not is_logged_in():
        return redirect(url_for('login'))

    role = session.get('role')

    if role == 'admin':
        return redirect(url_for('admin_dashboard'))
    elif role == 'student':
        # يمكن تفعيل إعادة التوجيه القسري لتفضيلات أولية عبر إعداد STRICT_MANDATORY_ONBOARDING
        if STRICT_MANDATORY_ONBOARDING and not has_completed_onboarding(session['user_id']):
            flash("يجب إكمال ملف التفضيلات أولاً للوصول للوحة التحكم", "warning")
            return redirect(url_for('student_onboarding'))

        return redirect(url_for('student_dashboard'))
    elif role == 'tutor':
        if STRICT_MANDATORY_ONBOARDING and not has_completed_tutor_profile(session['user_id']):
            flash("يرجى تحديث ملف المعلم أولاً للوصول للوحة المعلم.", "warning")
            return redirect(url_for('settings'))
        return redirect(url_for('dashboard'))

    session.clear()
    flash("Invalid account role.", "danger")
    return redirect(url_for('login'))
    

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

# -------------------------
# لوحة تحكم الادمن ب
# -------------------------
@app.route('/admin_dashboard')
def admin_dashboard():
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT COUNT(*) AS total_students FROM students")
        total_students = cursor.fetchone()['total_students']

        cursor.execute("SELECT COUNT(*) AS total_tutors FROM tutors")
        total_tutors = cursor.fetchone()['total_tutors']

        cursor.execute("SELECT COUNT(*) AS total_sessions FROM sessions")
        total_sessions = cursor.fetchone()['total_sessions']

        cursor.execute("SELECT COUNT(*) AS pending_sessions FROM sessions WHERE status='pending'")
        pending_sessions = cursor.fetchone()['pending_sessions']

        cursor.execute("SELECT COUNT(*) AS total_assignments FROM assignments")
        total_assignments = cursor.fetchone()['total_assignments']

        cursor.execute("SELECT COUNT(*) AS total_resources FROM resources")
        total_resources = cursor.fetchone()['total_resources']

        cursor.execute("""
            SELECT id, name, email, profile_pic
            FROM students
            ORDER BY id DESC
            LIMIT 5
        """)
        latest_students = cursor.fetchall()

        cursor.execute("""
            SELECT id, name, email, subject, profile_pic
            FROM tutors
            ORDER BY id DESC
            LIMIT 5
        """)
        latest_tutors = cursor.fetchall()

        cursor.execute("""
            SELECT s.id, s.status, s.session_date, s.meeting_link,
                   st.name AS student_name,
                   t.name AS tutor_name
            FROM sessions s
            JOIN students st ON s.student_id = st.id
            JOIN tutors t ON s.tutor_id = t.id
            ORDER BY s.id DESC
            LIMIT 8
        """)
        latest_sessions = cursor.fetchall()

    finally:
        cursor.close()
        conn.close()

    stats = {
        'total_students': total_students,
        'total_tutors': total_tutors,
        'total_sessions': total_sessions,
        'pending_sessions': pending_sessions,
        'total_assignments': total_assignments,
        'total_resources': total_resources
    }

    return render_template(
        'admin_dashboard.html',
        stats=stats,
        latest_students=latest_students,
        latest_tutors=latest_tutors,
        latest_sessions=latest_sessions
    )
# -------------------------
# إدارة الطلاب للادمن
# -------------------------
@app.route('/admin/students', methods=['GET', 'POST'])
def admin_students():
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if request.method == 'POST':
            name = request.form.get('name')
            email = request.form.get('email')
            password = request.form.get('password')

            cursor.execute(
                "INSERT INTO users (email, password, role) VALUES (%s, %s, 'student')",
                (email, password)
            )
            user_id = cursor.lastrowid

            cursor.execute(
                "INSERT INTO students (id, name, email) VALUES (%s, %s, %s)",
                (user_id, name, email)
            )

            conn.commit()
            flash("تم إضافة الطالب بنجاح", "success")
            return redirect(url_for('admin_students'))

        cursor.execute("SELECT * FROM students ORDER BY id DESC")
        students = cursor.fetchall()

    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"خطأ في قاعدة البيانات: {err}", "danger")
        students = []

    finally:
        cursor.close()
        conn.close()

    return render_template('admin_students.html', students=students)


@app.route('/admin/students/delete/<int:student_id>')
def delete_student_admin(student_id):
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM users WHERE id=%s AND role='student'", (student_id,))
        conn.commit()
        flash("تم حذف الطالب بنجاح", "info")
    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"فشل حذف الطالب: {err}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin_students'))

# -------------------------
# إدارة المعلمين
# -------------------------
@app.route('/admin/tutors', methods=['GET', 'POST'])
def admin_tutors():
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if request.method == 'POST':
            name = request.form.get('name')
            email = request.form.get('email')
            password = request.form.get('password')
            subject = request.form.get('subject')
            experience = request.form.get('experience') or 0
            rating = request.form.get('rating') or 0
            available_time = request.form.get('available_time') or ''
            bio = request.form.get('bio') or ''
            hourly_rate = request.form.get('hourly_rate') or 10

            cursor.execute(
                "INSERT INTO users (email, password, role) VALUES (%s, %s, 'tutor')",
                (email, password)
            )
            user_id = cursor.lastrowid

            cursor.execute("""
                INSERT INTO tutors
                (id, name, email, subject, experience, rating, available_time, bio, hourly_rate)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (user_id, name, email, subject, experience, rating, available_time, bio, hourly_rate))

            conn.commit()
            flash("تم إضافة المعلم بنجاح", "success")
            return redirect(url_for('admin_tutors'))

        cursor.execute("SELECT * FROM tutors ORDER BY id DESC")
        tutors = cursor.fetchall()

    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"خطأ في قاعدة البيانات: {err}", "danger")
        tutors = []

    finally:
        cursor.close()
        conn.close()

    return render_template('admin_tutors.html', tutors=tutors)


@app.route('/admin/tutors/delete/<int:tutor_id>')
def delete_tutor_admin(tutor_id):
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM users WHERE id=%s AND role='tutor'", (tutor_id,))
        conn.commit()
        flash("تم حذف المعلم بنجاح", "info")
    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"فشل حذف المعلم: {err}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin_tutors'))

# -------------------------
# لوحة إدارة الجلسات داخل ب
# -------------------------
@app.route('/admin/sessions')
def admin_sessions():
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT s.id, s.student_id, s.tutor_id, s.session_date, s.status, s.meeting_link,
                   st.name AS student_name,
                   t.name AS tutor_name
            FROM sessions s
            JOIN students st ON s.student_id = st.id
            JOIN tutors t ON s.tutor_id = t.id
            ORDER BY s.id DESC
        """)
        sessions = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    return render_template('admin_sessions.html', sessions=sessions)


@app.route('/admin/sessions/approve/<int:session_id>')
def approve_session_admin(session_id):
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "UPDATE sessions SET status='confirmed' WHERE id=%s",
            (session_id,)
        )
        conn.commit()
        flash("تم تأكيد الجلسة بنجاح", "success")
    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"فشل تأكيد الجلسة: {err}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin_sessions'))


@app.route('/admin/sessions/pending/<int:session_id>')
def pending_session_admin(session_id):
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "UPDATE sessions SET status='pending' WHERE id=%s",
            (session_id,)
        )
        conn.commit()
        flash("تم تحويل حالة الجلسة إلى pending", "info")
    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"فشل تحديث الجلسة: {err}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin_sessions'))


@app.route('/admin/sessions/delete/<int:session_id>')
def delete_session_admin(session_id):
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM sessions WHERE id=%s", (session_id,))
        conn.commit()
        flash("تم حذف الجلسة بنجاح", "info")
    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"فشل حذف الجلسة: {err}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin_sessions'))

# -------------------------
# لوحة إدارة الواجبات داخل ب
# -------------------------
@app.route('/admin/assignments', methods=['GET', 'POST'])
def admin_assignments():
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if request.method == 'POST':
            tutor_id = request.form.get('tutor_id')
            student_id = request.form.get('student_id') or None
            title = request.form.get('title')
            description = request.form.get('description')
            due_date = request.form.get('due_date')

            cursor.execute("""
                INSERT INTO assignments (tutor_id, student_id, title, description, due_date)
                VALUES (%s, %s, %s, %s, %s)
            """, (tutor_id, student_id, title, description, due_date))

            conn.commit()
            flash("تمت إضافة الواجب بنجاح", "success")
            return redirect(url_for('admin_assignments'))

        cursor.execute("SELECT id, name FROM tutors ORDER BY name ASC")
        tutors = cursor.fetchall()

        cursor.execute("SELECT id, name FROM students ORDER BY name ASC")
        students = cursor.fetchall()

        cursor.execute("""
            SELECT a.*,
                   t.name AS tutor_name,
                   s.name AS student_name
            FROM assignments a
            LEFT JOIN tutors t ON a.tutor_id = t.id
            LEFT JOIN students s ON a.student_id = s.id
            ORDER BY a.id DESC
        """)
        assignments = cursor.fetchall()

    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"خطأ في قاعدة البيانات: {err}", "danger")
        assignments = []
        tutors = []
        students = []

    finally:
        cursor.close()
        conn.close()

    return render_template(
        'admin_assignments.html',
        assignments=assignments,
        tutors=tutors,
        students=students
    )


@app.route('/admin/assignments/delete/<int:assignment_id>')
def delete_assignment_admin(assignment_id):
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM assignments WHERE id=%s", (assignment_id,))
        conn.commit()
        flash("تم حذف الواجب بنجاح", "info")
    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"فشل حذف الواجب: {err}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin_assignments'))

# -------------------------
# لوحةإدارة الموارد ب
# -------------------------
@app.route('/admin/resources', methods=['GET', 'POST'])
def admin_resources():
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if request.method == 'POST':
            tutor_id = request.form.get('tutor_id')
            title = request.form.get('title')
            description = request.form.get('description')
            link = request.form.get('link')
            subject = request.form.get('subject')

            cursor.execute("""
                INSERT INTO resources (tutor_id, title, description, link, subject)
                VALUES (%s, %s, %s, %s, %s)
            """, (tutor_id, title, description, link, subject))

            conn.commit()
            flash("تمت إضافة المورد بنجاح", "success")
            return redirect(url_for('admin_resources'))

        cursor.execute("SELECT id, name FROM tutors ORDER BY name ASC")
        tutors = cursor.fetchall()

        cursor.execute("""
            SELECT r.*,
                   t.name AS tutor_name
            FROM resources r
            LEFT JOIN tutors t ON r.tutor_id = t.id
            ORDER BY r.id DESC
        """)
        resources = cursor.fetchall()

    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"خطأ في قاعدة البيانات: {err}", "danger")
        resources = []
        tutors = []

    finally:
        cursor.close()
        conn.close()

    return render_template(
        'admin_resources.html',
        resources=resources,
        tutors=tutors
    )


@app.route('/admin/resources/delete/<int:resource_id>')
def delete_resource_admin(resource_id):
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM resources WHERE id=%s", (resource_id,))
        conn.commit()
        flash("تم حذف المورد بنجاح", "info")
    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"فشل حذف المورد: {err}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin_resources'))

# -------------------------
# لوحة إدارة الأسئلة ب
# -------------------------
@app.route('/admin/questions', methods=['GET', 'POST'])
def admin_questions():
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if request.method == 'POST':
            tutor_id = request.form.get('tutor_id')
            question = request.form.get('question')
            answer = request.form.get('answer')

            cursor.execute("""
                INSERT INTO questions (tutor_id, question, answer)
                VALUES (%s, %s, %s)
            """, (tutor_id, question, answer))

            conn.commit()
            flash("تمت إضافة السؤال بنجاح", "success")
            return redirect(url_for('admin_questions'))

        cursor.execute("SELECT id, name FROM tutors ORDER BY name ASC")
        tutors = cursor.fetchall()

        cursor.execute("""
            SELECT q.*,
                   t.name AS tutor_name
            FROM questions q
            LEFT JOIN tutors t ON q.tutor_id = t.id
            ORDER BY q.id DESC
        """)
        questions = cursor.fetchall()

    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"خطأ في قاعدة البيانات: {err}", "danger")
        questions = []
        tutors = []

    finally:
        cursor.close()
        conn.close()

    return render_template(
        'admin_questions.html',
        questions=questions,
        tutors=tutors
    )


@app.route('/admin/questions/delete/<int:question_id>')
def delete_question_admin(question_id):
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM questions WHERE id=%s", (question_id,))
        conn.commit()
        flash("تم حذف السؤال بنجاح", "info")
    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"فشل حذف السؤال: {err}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin_questions'))

# -------------------------
# لوحة إدارة المراجعات ب
# -------------------------
@app.route('/admin/reviews', methods=['GET', 'POST'])
def admin_reviews():
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if request.method == 'POST':
            student_id = request.form.get('student_id')
            tutor_id = request.form.get('tutor_id')
            rating = request.form.get('rating')
            comment = request.form.get('comment')

            cursor.execute("""
                INSERT INTO reviews (student_id, tutor_id, rating, comment)
                VALUES (%s, %s, %s, %s)
            """, (student_id, tutor_id, rating, comment))

            conn.commit()
            flash("تمت إضافة المراجعة بنجاح", "success")
            return redirect(url_for('admin_reviews'))

        cursor.execute("SELECT id, name FROM students ORDER BY name ASC")
        students = cursor.fetchall()

        cursor.execute("SELECT id, name FROM tutors ORDER BY name ASC")
        tutors = cursor.fetchall()

        cursor.execute("""
            SELECT r.*,
                   s.name AS student_name,
                   t.name AS tutor_name
            FROM reviews r
            LEFT JOIN students s ON r.student_id = s.id
            LEFT JOIN tutors t ON r.tutor_id = t.id
            ORDER BY r.id DESC
        """)
        reviews = cursor.fetchall()

    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"خطأ في قاعدة البيانات: {err}", "danger")
        reviews = []
        students = []
        tutors = []

    finally:
        cursor.close()
        conn.close()

    return render_template(
        'admin_reviews.html',
        reviews=reviews,
        students=students,
        tutors=tutors
    )


@app.route('/admin/reviews/delete/<int:review_id>')
def delete_review_admin(review_id):
    if not admin_only():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM reviews WHERE id=%s", (review_id,))
        conn.commit()
        flash("تم حذف المراجعة بنجاح", "info")
    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"فشل حذف المراجعة: {err}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin_reviews'))

# -------------------------
# لوحة تحكم الطالب
# -------------------------

@app.route('/student_dashboard')
def student_dashboard():
    if not student_only():
        return redirect(url_for('dashboard_router'))

    user_id = session['user_id']

    # Continue to dashboard even if onboarding is incomplete.
    # The template shows a banner with {
    #   'complete profile now' CTA and 'update preferences' link.
    # }
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # 1. بيانات الطالب الأساسية والرصيد
        cursor.execute("SELECT * FROM students WHERE id=%s", (user_id,))
        student_data = cursor.fetchone() or {}
        # ensure no None for optional fields
        if student_data is not None:
            student_data['preferred_subjects'] = (student_data.get('preferred_subjects') or '').strip()
            student_data['preferred_times'] = (student_data.get('preferred_times') or '').strip()
            student_data['profile_pic'] = (student_data.get('profile_pic') or '').strip()

        # Get student preferences for personalized recommendations
        cursor.execute("SELECT * FROM student_preferences WHERE student_id=%s", (user_id,))
        preferences = cursor.fetchone() or {}
        preferences['preferred_subjects'] = (preferences.get('preferred_subjects') or '').strip()
        preferences['preferred_times'] = (preferences.get('preferred_times') or '').strip()
        preferences['educational_level'] = (preferences.get('educational_level') or '').strip()
        preferences['teaching_style'] = (preferences.get('teaching_style') or '').strip()

        preferred_subject = ''
        if preferences and preferences.get('preferred_subjects'):
            preferred_subject = preferences['preferred_subjects'].split(',')[0].strip()

        # Build best-match tutor recommendations from student preferences
        best_match_tutors = []
        matched_subjects = []
        remaining_subjects = []

        if preferences and preferences.get('preferred_subjects'):
            preferred_subjects = [s.strip() for s in preferences['preferred_subjects'].split(',') if s.strip()]

            for sub in preferred_subjects:
                cursor.execute("SELECT COUNT(*) as cnt FROM tutors WHERE subject LIKE %s", (f"%{sub}%",))
                count_res = cursor.fetchone()
                if count_res and count_res.get('cnt', 0) > 0:
                    matched_subjects.append(sub)
                else:
                    remaining_subjects.append(sub)

            if preferred_subject:
                best_match_tutors, _, _ = recommend_tutors(user_id, preferred_subject, limit=5)
        else:
            # if preferences are incomplete, do not provide automated best-match list
            best_match_tutors = []


        # 2. قائمة المعلمين (لعرضهم في قسم الاكتشاف)
        cursor.execute("SELECT id, name, subject, bio, hourly_rate, profile_pic FROM tutors")
        tutors_list = cursor.fetchall()

        # 3. الجلسات القادمة (إحصائية + قائمة)
        cursor.execute("""
            SELECT s.*, t.name as tutor_name
            FROM sessions s
            JOIN tutors t ON s.tutor_id = t.id
            WHERE s.student_id=%s
            ORDER BY s.session_date DESC
        """, (user_id,))
        sessions_list = cursor.fetchall()
        upcoming_sessions_count = len([s for s in sessions_list if s['status'] == 'confirmed'])

        # 4. الواجبات وحساب المعلق منها (إحصائية + قائمة)
        cursor.execute("""
            SELECT a.*, 
                   t.name AS tutor_name,
                   s.status AS submission_status
            FROM assignments a
            JOIN tutors t ON a.tutor_id = t.id
            LEFT JOIN assignment_submissions s
                ON a.id = s.assignment_id AND s.student_id = %s
            WHERE a.student_id=%s OR a.student_id IS NULL
            ORDER BY a.due_date ASC
        """, (user_id, user_id))
        assignments_list = cursor.fetchall()
        # حساب الواجبات التي لم يتم تسليمها بعد
        pending_assignments_count = len([a for a in assignments_list if not a['submission_status']])

        # 5. المصادر التعليمية (Resources)
        cursor.execute("""
            SELECT r.*, t.name as tutor_name
            FROM resources r
            JOIN tutors t ON r.tutor_id = t.id
            ORDER BY r.id DESC LIMIT 10
        """)
        resources_list = cursor.fetchall()

        # 6. الدرجات والاختبارات
        cursor.execute("""
            SELECT g.*, a.title AS assignment_title
            FROM grades g
            LEFT JOIN assignments a ON g.assignment_id = a.id
            WHERE g.student_id=%s
            ORDER BY g.grade_date DESC
        """, (user_id,))
        grades_list = cursor.fetchall()

        # 7. الاختبارات (Quizzes)
        cursor.execute("""
            SELECT q.*, qr.score, qr.total
            FROM quizzes q
            LEFT JOIN quiz_results qr ON q.id = qr.quiz_id AND qr.student_id = %s
            ORDER BY q.id DESC
        """, (user_id,))
        quizzes_list = cursor.fetchall()

        # 8. الإشعارات غير المقروءة
        cursor.execute("SELECT * FROM notifications WHERE student_id=%s AND is_read=FALSE ORDER BY id DESC", (user_id,))
        notifications_list = cursor.fetchall()

    except Exception as e:
        print(f"Error in dashboard: {e}")
        flash("حدث خطأ أثناء تحميل بيانات لوحة التحكم", "danger")
        return redirect(url_for('index'))
    
    finally:
        cursor.close()
        conn.close()

    # حصر أفضل 3 معلمين مع بيانات التوافق المرئية
    best_matches_top3 = best_match_tutors[:3] if best_match_tutors else []

    # تمرير كل البيانات للـ HTML بما فيها المتغيرات الجديدة للإحصائيات
    return render_template(
        "student_dashboard.html",
        student=student_data,
        tutors=tutors_list,
        sessions=sessions_list,
        assignments=assignments_list,
        resources=resources_list,
        grades=grades_list,
        quizzes=quizzes_list,
        notifications=notifications_list,
        pending_count=pending_assignments_count,   # متغير جديد للبطاقات
        sessions_count=upcoming_sessions_count,    # متغير جديد للبطاقات
        best_matches=best_match_tutors,
        best_matches_top3=best_matches_top3,
        preferences=preferences,
        matched_subjects=matched_subjects if 'matched_subjects' in locals() else [],
        remaining_subjects=remaining_subjects if 'remaining_subjects' in locals() else []
    )

@app.route('/student_matches')
def student_matches():
    if not student_only():
        return jsonify({'error': 'unauthorized'}), 401

    user_id = session['user_id']

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM student_preferences WHERE student_id=%s", (user_id,))
        preferences = cursor.fetchone()

        if not preferences or not preferences.get('preferred_subjects'):
            return jsonify({
                'matched_subjects': [],
                'remaining_subjects': [],
                'best_matches': []
            })

        preferred_subjects = [s.strip() for s in preferences['preferred_subjects'].split(',') if s.strip()]

        matched_subjects = []
        remaining_subjects = []

        for sub in preferred_subjects:
            cursor.execute("SELECT COUNT(*) as cnt FROM tutors WHERE subject LIKE %s", (f"%{sub}%",))
            count_res = cursor.fetchone()
            if count_res and count_res.get('cnt', 0) > 0:
                matched_subjects.append(sub)
            else:
                remaining_subjects.append(sub)

        best_matches, _, _ = recommend_tutors(user_id, preferred_subjects[0], limit=5) if preferred_subjects else ([], None, None)

        best_matches_simple = [
            {
                'tutor_id': item['tutor']['id'],
                'name': item['tutor']['name'],
                'subject': item['tutor']['subject'],
                'score': item['score'],
                'confidence': item['confidence'],
                'reasons': item['reasons']
            } for item in best_matches
        ]

        result = {
            'matched_subjects': matched_subjects,
            'remaining_subjects': remaining_subjects,
            'best_matches': best_matches_simple,
            'best_matches_top3': best_matches_simple[:3]
        }
        return jsonify(result)
    finally:
        cursor.close()
        conn.close()

# -------------------------
# إعداد تفضيلات الطالب (Student Onboarding)
# -------------------------

@app.route('/student_onboarding')
def student_onboarding():
    """Show onboarding form for new students"""
    if not student_only():
        return redirect(url_for('dashboard_router'))
    
    student_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        # Check if student already has preferences
        cursor.execute("SELECT id FROM student_preferences WHERE student_id=%s", (student_id,))
        has_prefs = cursor.fetchone()
        
        if has_prefs:
            # Already completed onboarding
            return redirect(url_for('student_dashboard'))
        
    finally:
        cursor.close()
        conn.close()
    
    return render_template('student_onboarding.html')

@app.route('/save_student_preferences', methods=['POST'])
def save_student_preferences():
    """Save student preferences to database"""
    if not student_only():
        return redirect(url_for('dashboard_router'))
    
    student_id = session['user_id']
    
    try:
        educational_level = request.form.get('educational_level', '')
        subjects_list = request.form.get('subjects_list', '')
        preferred_times_list = request.form.get('preferred_times_list', '')
        max_hourly_rate = request.form.get('max_hourly_rate', 0)
        teaching_style = request.form.get('teaching_style', '')
        
        # Validate required fields
        if not all([educational_level, subjects_list, preferred_times_list, max_hourly_rate, teaching_style]):
            flash("يرجى ملء جميع الحقول المطلوبة", "danger")
            return redirect(url_for('student_onboarding'))
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            # Check if preferences already exist
            cursor.execute("SELECT id FROM student_preferences WHERE student_id=%s", (student_id,))
            existing = cursor.fetchone()
            
            if existing:
                # Update existing preferences
                cursor.execute("""
                    UPDATE student_preferences
                    SET educational_level=%s,
                        preferred_subjects=%s,
                        preferred_times=%s,
                        max_hourly_rate=%s,
                        teaching_style=%s,
                        updated_at=NOW()
                    WHERE student_id=%s
                """, (educational_level, subjects_list, preferred_times_list, max_hourly_rate, teaching_style, student_id))
                flash_msg = "تم تحديث التفضيلات بنجاح ✅"
            else:
                # Insert new preferences
                cursor.execute("""
                    INSERT INTO student_preferences
                    (student_id, educational_level, preferred_subjects, preferred_times, max_hourly_rate, teaching_style)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (student_id, educational_level, subjects_list, preferred_times_list, max_hourly_rate, teaching_style))
                flash_msg = "تم حفظ التفضيلات بنجاح ✅"
            
            conn.commit()
            flash(flash_msg, "success")
            
        except mysql.connector.Error as err:
            conn.rollback()
            flash(f"خطأ في قاعدة البيانات: {err}", "danger")
            return redirect(url_for('student_onboarding'))
        
        finally:
            cursor.close()
            conn.close()
        
        return redirect(url_for('student_dashboard'))
    
    except Exception as e:
        flash(f"حدث خطأ: {str(e)}", "danger")
        return redirect(url_for('student_onboarding'))

@app.route('/edit_preferences')
def edit_preferences():
    """Allow students to edit their preferences"""
    if not student_only():
        return redirect(url_for('dashboard_router'))
    
    student_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        cursor.execute("SELECT * FROM student_preferences WHERE student_id=%s", (student_id,))
        preferences = cursor.fetchone()
        
        if not preferences:
            flash("لم تقم بملء التفضيلات بعد", "warning")
            return redirect(url_for('student_onboarding'))
        
        return render_template('student_onboarding.html', preferences=preferences)
    
    finally:
        cursor.close()
        conn.close()


@app.route('/tutor_onboarding', methods=['GET', 'POST'])
def tutor_onboarding():
    """Onboarding wizard for tutors"""
    if not tutor_only():
        return redirect(url_for('dashboard_router'))

    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if request.method == 'POST':
            subject = request.form.get('subject', '').strip()
            experience_level = request.form.get('experience_level', '').strip()
            teaching_style = request.form.get('teaching_style', '').strip()
            available_time = request.form.get('available_time', '').strip()
            hourly_rate = request.form.get('hourly_rate', '').strip()
            bio = request.form.get('bio', '').strip()

            if not all([subject, experience_level, teaching_style, available_time, hourly_rate]):
                flash('يرجى ملء جميع الحقول المطلوبة لإكمال ملف المعلم.', 'danger')
                return redirect(url_for('tutor_onboarding'))

            try:
                hourly_rate_value = float(hourly_rate)
            except ValueError:
                flash('سعر الساعة يجب أن يكون رقمًا صالحًا.', 'danger')
                return redirect(url_for('tutor_onboarding'))

            cursor.execute(
                'UPDATE tutors SET subject=%s, experience_level=%s, teaching_style=%s, available_time=%s, hourly_rate=%s, bio=%s WHERE id=%s',
                (subject, experience_level, teaching_style, available_time, hourly_rate_value, bio, user_id)
            )
            conn.commit()

            flash('تم تحديث ملف المعلم بنجاح! ستظهر في نتائج التوصية حالاً.', 'success')
            return redirect(url_for('dashboard'))

        cursor.execute('SELECT * FROM tutors WHERE id=%s', (user_id,))
        tutor_data = cursor.fetchone()
        return render_template('tutor_onboarding.html', tutor=tutor_data)

    finally:
        cursor.close()
        conn.close()


@app.route('/activate_recommendation/<int:tutor_id>', methods=['POST'])
def activate_recommendation(tutor_id):
    if not student_only():
        return redirect(url_for('dashboard_router'))

    student_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute('SELECT * FROM tutors WHERE id=%s', (tutor_id,))
        tutor = cursor.fetchone()
        if not tutor:
            flash('المعلم غير موجود.', 'danger')
            return redirect(url_for('student_dashboard'))

        if not has_completed_onboarding(student_id):
            flash('يرجى إكمال تفضيلاتك أولاً قبل تفعيل توصية المعلم.', 'warning')
            return redirect(url_for('student_onboarding'))

        cursor.execute('SELECT * FROM student_preferences WHERE student_id=%s', (student_id,))
        pref = cursor.fetchone()

        if not pref:
            flash('يرجى إكمال التفضيلات أولاً.', 'warning')
            return redirect(url_for('student_onboarding'))

        new_subjects = merge_csv_unique(pref.get('preferred_subjects', ''), tutor.get('subject', ''))
        new_times = merge_csv_unique(pref.get('preferred_times', ''), tutor.get('available_time', ''))

        cursor.execute(
            'UPDATE student_preferences SET preferred_subjects=%s, preferred_times=%s WHERE student_id=%s',
            (new_subjects, new_times, student_id)
        )
        conn.commit()

        flash(f'تم تفعيل التوصية للمعلم {tutor.get("name")} وتم تحديث تفضيلاتك تلقائياً.', 'success')
        return redirect(url_for('student_dashboard'))

    finally:
        cursor.close()
        conn.close()


# -------------------------
# لوحة تحكم المعلم
# -------------------------

@app.route('/dashboard')
def dashboard():

    if not tutor_only():
        return redirect(url_for('dashboard_router'))

    user_id = session['user_id']
    active_section = request.args.get('section', 'main')

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:

        cursor.execute("SELECT * FROM tutors WHERE id=%s", (user_id,))
        tutor_data = cursor.fetchone()

        if tutor_data and not has_completed_tutor_profile(user_id):
            flash("لا يزال ملفك الشخصي غير مكتمل. أضف التفاصيل المهنية وسيتم ظهورك كتوصية لكثير من الطلاب.", "info")

        # حساب الرصيد
        cursor.execute(
            "SELECT COUNT(*) as confirmed_count FROM sessions WHERE tutor_id=%s AND status='confirmed'",
            (user_id,)
        )
        confirmed_count = cursor.fetchone()['confirmed_count'] or 0
        current_balance = confirmed_count * 10

        section_data = {
            'balance': current_balance,
            'sessions': [],
            'questions': [],
            'assignments': [],
            'submissions': [],
            'quiz_results': []
        }

        if active_section == 'q_bank':

            cursor.execute(
                "SELECT id, question, answer FROM questions WHERE tutor_id=%s",
                (user_id,)
            )
            section_data['questions'] = cursor.fetchall()

        elif active_section == 'assignments':

            cursor.execute(
                "SELECT * FROM assignments WHERE tutor_id=%s",
                (user_id,)
            )
            section_data['assignments'] = cursor.fetchall()

        
        elif active_section == 'submissions':

            cursor.execute("""
                SELECT s.id AS submission_id,
                       s.assignment_id,
                       s.student_id,
                       s.submission_text,
                       s.submission_file,
                       s.submitted_at,
                       s.status,
                       a.title AS assignment_title,
                       st.name AS student_name
                FROM assignment_submissions s
                JOIN assignments a ON s.assignment_id = a.id
                JOIN students st ON s.student_id = st.id
                WHERE a.tutor_id=%s
                ORDER BY s.submitted_at DESC
            """, (user_id,))

            section_data['submissions'] = cursor.fetchall()
            
        elif active_section == 'quiz_results':

            cursor.execute("""
             SELECT 
                qr.id,
                qr.score,
                qr.total,
                qr.created_at,
                st.name AS student_name,
                qz.title AS quiz_title
            FROM quiz_results qr
            JOIN students st ON qr.student_id = st.id
            JOIN quizzes qz ON qr.quiz_id = qz.id
            WHERE qz.tutor_id = %s
         ORDER BY qr.created_at DESC
    """, (user_id,))

            section_data['quiz_results'] = cursor.fetchall()    

        else:

            cursor.execute("""
                SELECT s.*, st.name as student_name
                FROM sessions s
                JOIN students st ON s.student_id = st.id
                WHERE s.tutor_id=%s
                ORDER BY s.id DESC
            """, (user_id,))

            section_data['sessions'] = cursor.fetchall()

    finally:
        cursor.close()
        conn.close()

    return render_template(
        "tutor_dashboard.html",
        tutor=tutor_data,
        active_section=active_section,
        data=section_data
    )
# -------------------------
# Actions للمعلم (حماية role)
# -------------------------

@app.route('/approve_session/<int:session_id>')
def approve_session(session_id):
    if not tutor_only():
        return redirect(url_for('dashboard_router'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
    "UPDATE sessions SET status='confirmed' WHERE id=%s AND tutor_id=%s",
    (session_id, session['user_id'])
)
        conn.commit()
        flash("تم قبول الطلب وإضافته للمحفظة", "success")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard', section='main'))


@app.route('/delete_session/<int:session_id>')
def delete_session(session_id):
    if not tutor_only():
        return redirect(url_for('dashboard_router'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM sessions WHERE id=%s AND tutor_id=%s",
            (session_id, session['user_id'])
        )
        conn.commit()
        flash("تم حذف الطلب بنجاح", "info")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard', section='main'))

@app.route('/update_session_link/<int:session_id>', methods=['POST'])
def update_session_link(session_id):
    if not tutor_only():
        return redirect(url_for('dashboard_router'))

    meeting_link = request.form.get('meeting_link')

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE sessions SET meeting_link=%s WHERE id=%s AND tutor_id=%s",
            (meeting_link, session_id, session['user_id'])
        )
        conn.commit()
        flash("تم حفظ رابط الجلسة بنجاح", "success")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard', section='main'))

@app.route('/add_question', methods=['POST'])
def add_question():
    if not tutor_only():
        return redirect(url_for('dashboard_router'))

    q_text = request.form.get('question_text')
    ans = request.form.get('correct_answer')

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO questions (tutor_id, question, answer) VALUES (%s, %s, %s)",
            (session['user_id'], q_text, ans)
        )
        conn.commit()
        flash("تم إضافة السؤال بنجاح", "success")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard', section='q_bank'))


# ✅ هذا اللي كان يسبب المشكلة (تم إصلاحه)
@app.route('/add_assignment', methods=['POST'])
def add_assignment():
    if not tutor_only():
        flash("لازم تسجل دخول كمعلم عشان تنشر واجب", "danger")
        return redirect(url_for('dashboard_router'))

    title = request.form.get('title')
    due = request.form.get('due_date')
    description = request.form.get('description', '')
    tutor_id = session['user_id']

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM tutors WHERE id=%s", (tutor_id,))
        if not cursor.fetchone():
            flash("حساب المعلم غير موجود في جدول tutors. سجل دخول بحساب معلم صحيح.", "danger")
            return redirect(url_for('dashboard', section='assignments'))

        cursor.execute("""
            INSERT INTO assignments (tutor_id, title, due_date, description, student_id)
            VALUES (%s, %s, %s, %s, NULL)
        """, (tutor_id, title, due, description))

        conn.commit()
        flash("تم نشر الواجب لجميع الطلاب بنجاح", "success")

    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"خطأ في قاعدة البيانات: {err}", "danger")

    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard', section='assignments'))



@app.route('/grade_submission/<int:submission_id>', methods=['POST'])
def grade_submission(submission_id):
    if not tutor_only():
        return redirect(url_for('dashboard_router'))

    score = request.form.get('score')
    total_score = request.form.get('total_score')
    feedback = request.form.get('feedback', '').strip()
    tutor_id = session['user_id']

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT s.*, a.tutor_id
            FROM assignment_submissions s
            JOIN assignments a ON s.assignment_id = a.id
            WHERE s.id=%s AND a.tutor_id=%s
        """, (submission_id, tutor_id))
        submission = cursor.fetchone()

        if not submission:
            flash("لا يمكنك تقييم هذا التسليم.", "danger")
            return redirect(url_for('dashboard', section='submissions'))

        cursor.execute("""
            SELECT id FROM grades
            WHERE student_id=%s AND assignment_id=%s
        """, (submission['student_id'], submission['assignment_id']))
        existing_grade = cursor.fetchone()

        if existing_grade:
            cursor.execute("""
                UPDATE grades
                SET score=%s,
                    total_score=%s,
                    feedback=%s,
                    grade_date=NOW()
                WHERE student_id=%s AND assignment_id=%s
            """, (score, total_score, feedback, submission['student_id'], submission['assignment_id']))
        else:
            cursor.execute("""
                INSERT INTO grades (student_id, assignment_id, score, total_score, feedback, grade_date)
                VALUES (%s, %s, %s, %s, %s, NOW())
            """, (submission['student_id'], submission['assignment_id'], score, total_score, feedback))

        cursor.execute("""
            UPDATE assignment_submissions
            SET status='graded'
            WHERE id=%s
        """, (submission_id,))

        conn.commit()
        flash("تم تقييم الواجب بنجاح.", "success")

    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"خطأ في قاعدة البيانات: {err}", "danger")

    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard', section='submissions'))

# -------------------------
# حجز جلسة (Student) - ENHANCED with validation
# -------------------------

@app.route('/book_session', methods=['POST'])
def book_session():
    """Book a tutoring session with selected tutor and date/time"""
    if not student_only():
        return redirect(url_for('dashboard_router'))
    
    student_id = session['user_id']
    tutor_id = request.form.get('tutor_id')
    session_date = request.form.get('session_date')
    
    if not tutor_id or not session_date:
        flash("يرجى اختيار المعلم والتاريخ والوقت.", "danger")
        return redirect(url_for('student_dashboard'))
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Verify tutor exists before booking
        cursor.execute("SELECT id FROM tutors WHERE id = %s", (tutor_id,))
        if not cursor.fetchone():
            flash("المعلم المختار غير موجود.", "danger")
            return redirect(url_for('student_dashboard'))
        
        # Insert session into database
        cursor.execute("""
            INSERT INTO sessions (student_id, tutor_id, session_date, status)
            VALUES (%s, %s, %s, 'pending')
        """, (student_id, tutor_id, session_date))
        
        conn.commit()
        flash("تم حجز الجلسة بنجاح! سيتم التأكيد من قبل المعلم قريباً.", "success")
        
    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"خطأ في حجز الجلسة: {err}", "danger")
    
    finally:
        cursor.close()
        conn.close()
    
    return redirect(url_for('student_dashboard'))

@app.route('/submit_assignment/<int:assignment_id>', methods=['POST'])
def submit_assignment(assignment_id):
    if not student_only():
        return redirect(url_for('dashboard_router'))

    student_id = session['user_id']
    submission_text = request.form.get('submission_text', '').strip()
    submission_file_name = None

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # تحقق أن الواجب موجود ومتاح لهذا الطالب
        cursor.execute("""
            SELECT *
            FROM assignments
            WHERE id=%s AND (student_id=%s OR student_id IS NULL)
        """, (assignment_id, student_id))
        assignment = cursor.fetchone()

        if not assignment:
            flash("هذا الواجب غير متاح لك.", "danger")
            return redirect(url_for('student_dashboard'))

        # لو في ملف مرفوع
        if 'submission_file' in request.files:
            file = request.files['submission_file']
            if file and file.filename != '':
                filename = secure_filename(file.filename)
                submission_file_name = f"{student_id}_{assignment_id}_{filename}"
                file.save(os.path.join(app.config['SUBMISSIONS_FOLDER'], submission_file_name))

        # لازم يكون في نص أو ملف
        if not submission_text and not submission_file_name:
            flash("يجب كتابة حل أو رفع ملف قبل التسليم.", "danger")
            return redirect(url_for('student_dashboard'))

        # هل يوجد تسليم سابق؟
        cursor.execute("""
            SELECT id, status, submission_file
            FROM assignment_submissions
            WHERE assignment_id=%s AND student_id=%s
        """, (assignment_id, student_id))
        existing_submission = cursor.fetchone()

        if existing_submission:
            if existing_submission['status'] == 'graded':
                flash("لا يمكن تعديل هذا التسليم لأنه تم تقييمه بالفعل.", "danger")
                return redirect(url_for('student_dashboard'))

            old_file = existing_submission.get('submission_file')
            if submission_file_name is None:
                submission_file_name = old_file

            cursor.execute("""
                UPDATE assignment_submissions
                SET submission_text=%s,
                    submission_file=%s,
                    submitted_at=NOW(),
                    status='submitted'
                WHERE assignment_id=%s AND student_id=%s
            """, (submission_text, submission_file_name, assignment_id, student_id))

            flash("تم تحديث تسليم الواجب بنجاح.", "success")
        else:
            cursor.execute("""
                INSERT INTO assignment_submissions
                (assignment_id, student_id, submission_text, submission_file, submitted_at, status)
                VALUES (%s, %s, %s, %s, NOW(), 'submitted')
            """, (assignment_id, student_id, submission_text, submission_file_name))

            flash("تم تسليم الواجب بنجاح.", "success")

        conn.commit()

    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"خطأ في قاعدة البيانات: {err}", "danger")

    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('student_dashboard'))

# إعدادات المعلم

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if not tutor_only():
        return redirect(url_for('dashboard_router'))

    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if request.method == "POST":
            name = request.form.get('name')
            email = request.form.get('email')
            password = request.form.get('password')
            rate = request.form.get('hourly_rate')
            bio = request.form.get('bio')
            available_time = request.form.get('available_time')
            subject = request.form.get('subject')
            experience_level = request.form.get('experience_level')
            teaching_style = request.form.get('teaching_style')

            cursor.execute(
                "UPDATE tutors SET name=%s, email=%s, hourly_rate=%s, bio=%s, available_time=%s, subject=%s, experience_level=%s, teaching_style=%s WHERE id=%s",
                (name, email, rate, bio, available_time, subject, experience_level, teaching_style, user_id)
            )
            cursor.execute("UPDATE users SET email=%s WHERE id=%s", (email, user_id))
            if password:
                cursor.execute("UPDATE users SET password=%s WHERE id=%s", (password, user_id))

            if 'profile_pic' in request.files:
                file = request.files['profile_pic']
                if file and file.filename != '':
                    filename = secure_filename(file.filename)
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                    cursor.execute("UPDATE tutors SET profile_pic=%s WHERE id=%s", (filename, user_id))

            conn.commit()

            # Notify affected students that a tutor profile/curriculum has been updated
            notify_message = f"المعلم {name} حدّث ملفه الشخصي والمنهجية. افتح لوحة الطالب لمطابقة جديدة فورية."
            cursor.execute("SELECT s.id FROM students s JOIN student_preferences p ON s.id=p.student_id WHERE p.preferred_subjects LIKE %s", (f"%{subject}%",))
            interested_students = cursor.fetchall()
            for stud in interested_students:
                cursor.execute("INSERT INTO notifications (student_id, message) VALUES (%s, %s)", (stud['id'], notify_message))
            conn.commit()

            flash("تم تحديث الإعدادات", "success")
            return redirect(url_for('settings'))

        cursor.execute("SELECT * FROM tutors WHERE id=%s", (user_id,))
        tutor_info = cursor.fetchone()

    finally:
        cursor.close()
        conn.close()

    return render_template("settings.html", tutor=tutor_info)


# -------------------------
# إعدادات الطالب
# -------------------------

@app.route('/student_settings', methods=['GET', 'POST'])
def student_settings():
    if not student_only():
        return redirect(url_for('dashboard_router'))

    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if request.method == "POST":
            name = request.form.get('name')
            email = request.form.get('email')
            password = request.form.get('password')

            cursor.execute("UPDATE students SET name=%s, email=%s WHERE id=%s", (name, email, user_id))
            cursor.execute("UPDATE users SET email=%s WHERE id=%s", (email, user_id))
            if password:
                cursor.execute("UPDATE users SET password=%s WHERE id=%s", (password, user_id))

            if 'profile_pic' in request.files:
                file = request.files['profile_pic']
                if file and file.filename != '':
                    filename = secure_filename(file.filename)
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                    cursor.execute("UPDATE students SET profile_pic=%s WHERE id=%s", (filename, user_id))

            conn.commit()
            flash("تم تحديث الملف الشخصي بنجاح", "success")
            return redirect(url_for('student_settings'))

        cursor.execute("SELECT * FROM students WHERE id=%s", (user_id,))
        student_info = cursor.fetchone()

    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"حدث خطأ: {err}", "danger")
        student_info = None

    finally:
        cursor.close()
        conn.close()

    return render_template("student_settings.html", student=student_info)


# -------------------------
# حذف/تعديل أسئلة وواجبات (Tutor فقط)
# -------------------------

@app.route('/delete_question/<int:q_id>')
def delete_question(q_id):
    if not tutor_only():
        return redirect(url_for('dashboard_router'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM questions WHERE id=%s AND tutor_id=%s", (q_id, session['user_id']))
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard', section='q_bank'))


@app.route('/delete_assignment/<int:a_id>')
def delete_assignment(a_id):
    if not tutor_only():
        return redirect(url_for('dashboard_router'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM assignments WHERE id=%s AND tutor_id=%s", (a_id, session['user_id']))
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard', section='assignments'))

@app.route('/edit_assignment/<int:a_id>', methods=['POST'])
def edit_assignment(a_id):
    if not tutor_only():
        return redirect(url_for('dashboard_router'))

    title = request.form.get('title')
    due = request.form.get('due_date')

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE assignments SET title=%s, due_date=%s WHERE id=%s AND tutor_id=%s",
            (title, due, a_id, session['user_id'])
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard', section='assignments'))

# -------------------------
# إنشاء اختبار
# -------------------------
@app.route('/create_quiz', methods=['POST'])
def create_quiz():
    if not tutor_only():
        return redirect(url_for('dashboard_router'))

    title = request.form.get('title', '').strip()
    duration_minutes = request.form.get('duration_minutes', '').strip()
    question_ids = request.form.getlist('questions')

    if not title:
        flash("يرجى إدخال عنوان للاختبار.", "danger")
        return redirect(url_for('dashboard', section='q_bank'))

    if not duration_minutes.isdigit() or int(duration_minutes) <= 0:
        flash("يرجى إدخال مدة صحيحة للاختبار بالدقائق.", "danger")
        return redirect(url_for('dashboard', section='q_bank'))

    if not question_ids:
        flash("يرجى اختيار سؤال واحد على الأقل لإنشاء الاختبار.", "danger")
        return redirect(url_for('dashboard', section='q_bank'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "INSERT INTO quizzes (tutor_id, title, duration_minutes) VALUES (%s, %s, %s)",
            (session['user_id'], title, int(duration_minutes))
        )
        quiz_id = cursor.lastrowid

        for q in question_ids:
            cursor.execute(
                "INSERT INTO quiz_questions (quiz_id, question_id) VALUES (%s, %s)",
                (quiz_id, q)
            )

        cursor.execute("SELECT id FROM students")
        students = cursor.fetchall()

        for s in students:
            cursor.execute(
                "INSERT INTO notifications (student_id, message) VALUES (%s, %s)",
                (s[0], f"تم نشر اختبار جديد: {title}")
            )

        conn.commit()
        flash("تم نشر الاختبار بنجاح", "success")

    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"خطأ في قاعدة البيانات: {err}", "danger")

    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard', section='q_bank'))

# -------------------------
# الطالب يحل الاختبار
# -------------------------
@app.route('/take_quiz/<int:quiz_id>')
def take_quiz(quiz_id):
    if not student_only():
        return redirect(url_for('dashboard_router'))

    student_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT id, title, duration_minutes
            FROM quizzes
            WHERE id = %s
        """, (quiz_id,))
        quiz = cursor.fetchone()

        if not quiz:
            flash("الاختبار غير موجود.", "danger")
            return redirect(url_for('student_dashboard'))

        cursor.execute("""
            SELECT id
            FROM quiz_results
            WHERE quiz_id = %s AND student_id = %s
        """, (quiz_id, student_id))
        existing = cursor.fetchone()

        if existing:
            flash("لقد قمت بحل هذا الاختبار مسبقًا.", "info")
            return redirect(url_for('student_dashboard'))

        cursor.execute("""
            SELECT q.id, q.question
            FROM quiz_questions qq
            JOIN questions q ON qq.question_id = q.id
            WHERE qq.quiz_id = %s
        """, (quiz_id,))
        questions = cursor.fetchall()

        if not questions:
            flash("هذا الاختبار لا يحتوي على أسئلة.", "danger")
            return redirect(url_for('student_dashboard'))

    finally:
        cursor.close()
        conn.close()

    return render_template(
        "take_quiz.html",
        quiz=quiz,
        questions=questions,
        quiz_id=quiz_id
    )

# -------------------------
# تصحيح الاختبار تلقائي
# -------------------------
@app.route('/submit_quiz/<int:quiz_id>', methods=['POST'])
def submit_quiz(quiz_id):
    if not student_only():
        return redirect(url_for('dashboard_router'))

    student_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT id
            FROM quiz_results
            WHERE quiz_id = %s AND student_id = %s
        """, (quiz_id, student_id))
        existing_result = cursor.fetchone()

        if existing_result:
            flash("لقد قمت بحل هذا الاختبار مسبقًا.", "info")
            return redirect(url_for('student_dashboard'))

        cursor.execute("""
            SELECT q.id, q.answer
            FROM quiz_questions qq
            JOIN questions q ON qq.question_id = q.id
            WHERE qq.quiz_id = %s
        """, (quiz_id,))
        questions = cursor.fetchall()

        if not questions:
            flash("الاختبار غير موجود أو لا يحتوي على أسئلة.", "danger")
            return redirect(url_for('student_dashboard'))

        score = 0
        total = len(questions)

        for q in questions:
            student_answer = request.form.get(f"q_{q['id']}", '').strip()
            correct_answer = (q['answer'] or '').strip()

            if student_answer.lower() == correct_answer.lower():
                score += 1

        cursor.execute("""
            INSERT INTO quiz_results (quiz_id, student_id, score, total)
            VALUES (%s, %s, %s, %s)
        """, (quiz_id, student_id, score, total))

        conn.commit()

    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"خطأ في قاعدة البيانات: {err}", "danger")
        return redirect(url_for('student_dashboard'))

    finally:
        cursor.close()
        conn.close()

    return render_template(
        "quiz_result.html",
        score=score,
        total=total
    )
@app.route('/recommend')
def recommend():
    if not student_only():
        return redirect(url_for('dashboard_router'))
    
    student_id = session['user_id']
    subject = (request.args.get('subject') or '').strip()
    
    if not subject:
        flash("يرجى اختيار المادة أولاً للحصول على توصية.", "warning")
        return redirect(url_for('student_dashboard'))
    
    # Get multiple recommendations
    recommendations, level, avg_score = recommend_tutors(student_id, subject, limit=5)
    
    return render_template(
        'recommend.html',
        recommendations=recommendations,
        level=level,
        avg_score=round(avg_score, 1),
        subject=subject,
        primary_tutor=recommendations[0]['tutor'] if recommendations else None
    )

if __name__ == '__main__':
    app.run(debug=True, port=5050)
