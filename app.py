import streamlit as st
import sqlite3
import datetime
import re

# --- 1. 데이터베이스(DB) 초기화 및 설정 ---
def init_db():
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    # 숙제 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS homeworks 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, created_at TEXT)''')
    # 문제 이미지 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS questions 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, homework_id INTEGER, q_num INTEGER, image_data BLOB)''')
    # 학생 오답 제출 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS submissions 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, student_name TEXT, homework_id INTEGER, wrong_nums TEXT, submitted_at TEXT)''')
    conn.commit()
    conn.close()

init_db()

# 자연스러운 파일명 정렬 함수 (1.png, 2.png ... 10.png 순서 유지)
def natural_sort_key(file):
    numbers = re.findall(r'\d+', file.name)
    return int(numbers[0]) if numbers else file.name

# --- 2. DB 작업용 함수들 ---
def save_homework(title, uploaded_files):
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT INTO homeworks (title, created_at) VALUES (?, ?)", (title, now))
    hw_id = c.lastrowid
    
    sorted_files = sorted(uploaded_files, key=natural_sort_key)
    
    for idx, file in enumerate(sorted_files, start=1):
        image_bytes = file.read()
        c.execute("INSERT INTO questions (homework_id, q_num, image_data) VALUES (?, ?, ?)", 
                  (hw_id, idx, image_bytes))
        
    conn.commit()
    conn.close()
    return hw_id

def get_homeworks():
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute("SELECT id, title FROM homeworks ORDER BY id DESC")
    data = c.fetchall()
    conn.close()
    return data

def save_submission(student_name, hw_id, wrong_nums_list):
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    wrong_str = ",".join(map(str, sorted(wrong_nums_list)))
    c.execute("INSERT INTO submissions (student_name, homework_id, wrong_nums, submitted_at) VALUES (?, ?, ?, ?)",
              (student_name, hw_id, wrong_str, now))
    conn.commit()
    conn.close()

def get_submissions():
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute('''SELECT s.id, s.student_name, h.title, s.wrong_nums, s.submitted_at, s.homework_id 
                 FROM submissions s 
                 JOIN homeworks h ON s.homework_id = h.id 
                 ORDER BY s.id DESC''')
    data = c.fetchall()
    conn.close()
    return data

def get_question_count(hw_id):
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM questions WHERE homework_id = ?", (hw_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

def get_wrong_questions_images(hw_id, wrong_nums_list):
    if not wrong_nums_list:
        return []
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    placeholders = ','.join('?' for _ in wrong_nums_list)
    query = f"SELECT q_num, image_data FROM questions WHERE homework_id = ? AND q_num IN ({placeholders}) ORDER BY q_num"
    c.execute(query, [hw_id] + wrong_nums_list)
    data = c.fetchall()
    conn.close()
    return data

# --- 3. 화면 인쇄(PDF/프린터) 시 쓸 CSS 스타일 지정 ---
st.markdown("""
    <style>
    @media print {
        [data-testid="stSidebar"] { display: none !important; }
        header { display: none !important; }
        footer { display: none !important; }
        .stButton { display: none !important; }
        .no-print { display: none !important; }
    }
    </style>
""", unsafe_allow_html=True)

# --- 4. 메인 프로그램 화면 ---
st.title("📚 국어 오답노트 생성 시스템")

menu = st.sidebar.selectbox(
    "원하는 작업을 선택하세요",
    ["📝 [학생] 오답 체크하기", "📤 [선생님] 새 숙제 문제 등록", "🖨️ [선생님] 개인별 오답노트 인쇄"]
)

# -------------------------------------------------------------
# 메뉴 1: [학생] 오답 체크하기
# -------------------------------------------------------------
if menu == "📝 [학생] 오답 체크하기":
    st.subheader("📝 학생 오답 제출")
    
    homeworks = get_homeworks()
    if not homeworks:
        st.info("등록된 숙제가 없습니다. 선생님께 문의하세요.")
    else:
        hw_options = {f"{hw[1]}": hw[0] for hw in homeworks}
        selected_hw_title = st.selectbox("숙제를 선택하세요", list(hw_options.keys()))
        selected_hw_id = hw_options[selected_hw_title]
        
        student_name = st.text_input("이름을 입력하세요 (예: 홍길동)")
        
        total_q = get_question_count(selected_hw_id)
        st.write(f"총 문항 수: **{total_q}문제**")
        
        st.markdown("---")
        st.write("👇 **틀린 문제 번호를 모두 체크해 주세요.**")
        
        # 문제 번호를 5개씩 줄지어 깔끔하게 체크박스로 배치
        cols = st.columns(5)
        wrong_answers = []
        for i in range(1, total_q + 1):
            with cols[(i - 1) % 5]:
                if st.checkbox(f"{i}번", key=f"q_{i}"):
                    wrong_answers.append(i)
                    
        st.markdown("---")
        if st.button("제출하기"):
            if not student_name.strip():
                st.error("이름을 입력해 주세요.")
            elif not wrong_answers:
                st.warning("틀린 문제 번호를 하나 이상 선택해 주세요.")
            else:
                save_submission(student_name.strip(), selected_hw_id, wrong_answers)
                st.success(f"🎉 {student_name} 학생, 제출이 완료되었습니다! 오답 문제: {wrong_answers}")

# -------------------------------------------------------------
# 메뉴 2: [선생님] 새 숙제 문제 등록
# -------------------------------------------------------------
elif menu == "📤 [선생님] 새 숙제 문제 등록":
    st.subheader("📤 새 숙제 문제 등록")
    
    hw_title = st.text_input("숙제 이름 (예: 3월 2주차 문법 - 음운의 변동)")
    
    st.info("💡 Tip: 문제 이미지를 1.png, 2.png 또는 1.jpg, 2.jpg 처럼 문제 번호 순서대로 이름을 지어 한번에 업로드해 주세요.")
    uploaded_files = st.file_uploader("문제 이미지 파일들을 선택하세요", type=['png', 'jpg', 'jpeg'], accept_multiple_files=True)
    
    if st.button("숙제 등록 완료"):
        if not hw_title.strip():
            st.error("숙제 이름을 입력해 주세요.")
        elif not uploaded_files:
            st.error("문제 이미지 파일을 하나 이상 선택해 주세요.")
        else:
            hw_id = save_homework(hw_title.strip(), uploaded_files)
            st.success(f"✅ '{hw_title}' 등록 완료! 총 {len(uploaded_files)}문제가 저장되었습니다.")

# -------------------------------------------------------------
# 메뉴 3: [선생님] 개인별 오답노트 인쇄
# -------------------------------------------------------------
elif menu == "🖨️ [선생님] 개인별 오답노트 인쇄":
    st.subheader("🖨️ 제출된 학생 오답노트 출력")
    
    submissions = get_submissions()
    if not submissions:
        st.info("아직 학생들이 제출한 오답 내역이 없습니다.")
    else:
        sub_options = {f"{sub[1]} - {sub[2]} ({sub[4]})": sub for sub in submissions}
        selected_sub_title = st.selectbox("출력할 학생 제출 내역을 선택하세요", list(sub_options.keys()))
        selected_sub = sub_options[selected_sub_title]
        
        sub_id, student_name, hw_title, wrong_str, submitted_at, hw_id = selected_sub
        wrong_list = [int(n) for n in wrong_str.split(',')] if wrong_str else []
        
        st.markdown("---")
        
        # --- 오답노트 인쇄용 레이아웃 ---
        st.markdown(f"## 📄 맞춤 오답노트: {student_name} 학생")
        st.write(f"**숙제명:** {hw_title} | **제출일:** {submitted_at}")
        st.write(f"**다시 풀어볼 오답 번호:** {wrong_str}번")
        st.markdown("---")
        
        images = get_wrong_questions_images(hw_id, wrong_list)
        
        for q_num, img_bytes in images:
            st.markdown(f"### 📍 문제 {q_num}번")
            st.image(img_bytes, use_container_width=True)
            st.write("📝 **[풀이 / 정답 작성 공간]**")
            st.write("\n" * 3) # 풀이 공간 여백
            st.markdown("---")
            
        st.markdown("<div class='no-print'><b>💡 팁:</b> 웹브라우저에서 <b>Ctrl + P</b> (Mac은 <b>Cmd + P</b>)를 누르면 옆 메뉴바 없이 오답지 시험지만 깔끔하게 PDF로 저장하거나 인쇄할 수 있습니다.</div>", unsafe_allow_html=True)