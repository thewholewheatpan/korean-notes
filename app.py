import streamlit as st
import sqlite3
import datetime
import re
import fitz  # PyMuPDF (PDF 분석용)
from PIL import Image
import io

# --- 1. 데이터베이스(DB) 초기화 ---
def init_db():
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS homeworks 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS questions 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, homework_id INTEGER, q_num INTEGER, image_data BLOB)''')
    c.execute('''CREATE TABLE IF NOT EXISTS submissions 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, student_name TEXT, homework_id INTEGER, wrong_nums TEXT, submitted_at TEXT)''')
    conn.commit()
    conn.close()

init_db()

# 자연스러운 파일명 정렬 함수
def natural_sort_key(file):
    numbers = re.findall(r'\d+', file.name)
    return int(numbers[0]) if numbers else file.name

# --- 2. PDF 문제 자동 자르기 처리 함수 ---
def process_pdf_and_extract_questions(pdf_bytes):
    """
    PDF 파일 바이너리를 받아 문제 번호(1., 2. 등) 위치를 감지해 이미지 목록으로 반환
    반환값: [(문제번호, 이미지바이트), ...]
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    question_data = []

    # 1) 전체 페이지에서 문제 번호 좌표 추출
    for page_num in range(len(doc)):
        page = doc[page_num]
        blocks = page.get_text("blocks")
        for b in blocks:
            text = b[4].strip()
            # "1.", "01." 등 번호 패턴 인식
            match = re.match(r'^(\d{1,2})\.\s*', text)
            if match:
                q_num = int(match.group(1))
                question_data.append({
                    'q_num': q_num,
                    'page': page_num,
                    'y0': b[1],
                    'y1': b[3]
                })

    question_data.sort(key=lambda x: (x['page'], x['q_num']))
    extracted_questions = []

    # 2) 문제별 영역 크롭 및 PNG 변환
    for i, q in enumerate(question_data):
        q_num = q['q_num']
        page_num = q['page']
        page = doc[page_num]
        
        pix = page.get_pixmap(dpi=300)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        rect = page.rect
        scale_y = img.height / rect.height
        
        crop_top = max(0, int(q['y0'] * scale_y))
        
        # 다음 문제가 동일 페이지에 있을 경우 경계 설정
        if i < len(question_data) - 1 and question_data[i+1]['page'] == page_num:
            crop_bottom = min(img.height, int(question_data[i+1]['y0'] * scale_y))
        else:
            crop_bottom = img.height

        if crop_bottom > crop_top + 20:
            cropped_img = img.crop((0, crop_top, img.width, crop_bottom))
            img_byte_arr = io.BytesIO()
            cropped_img.save(img_byte_arr, format='PNG')
            extracted_questions.append((q_num, img_byte_arr.getvalue()))

    return extracted_questions

# --- 3. DB 작업용 함수들 ---
def save_homework_from_pdf(title, pdf_file):
    questions = process_pdf_and_extract_questions(pdf_file.read())
    if not questions:
        return None, 0
    
    conn = sqlite3.connect('wrong_answer_db.db')
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT INTO homeworks (title, created_at) VALUES (?, ?)", (title, now))
    hw_id = c.lastrowid
    
    for q_num, img_bytes in questions:
        c.execute("INSERT INTO questions (homework_id, q_num, image_data) VALUES (?, ?, ?)", 
                  (hw_id, q_num, img_bytes))
        
    conn.commit()
    conn.close()
    return hw_id, len(questions)

def save_homework_from_images(title, uploaded_files):
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

# --- 4. 인쇄용 CSS ---
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

# --- 5. 선생님 인증 암호 설정 (기본값: 1234) ---
TEACHER_PASSWORD = "1234"

st.title("📚 국어 오답노트 생성 시스템")

# 메뉴 구성
menu_options = ["📝 [학생] 오답 체크하기", "🔒 [선생님] 관리자 모드"]
selected_menu = st.sidebar.selectbox("원하는 작업을 선택하세요", menu_options)

# -------------------------------------------------------------
# 메뉴 1: [학생] 오답 체크하기 (기본 노출)
# -------------------------------------------------------------
if selected_menu == "📝 [학생] 오답 체크하기":
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
# 메뉴 2: [선생님] 관리자 모드 (비밀번호 인증 필요)
# -------------------------------------------------------------
elif selected_menu == "🔒 [선생님] 관리자 모드":
    st.subheader("🔒 선생님 전용 관리자 인증")
    
    password = st.sidebar.text_input("선생님 비밀번호 입력", type="password")
    
    if password != TEACHER_PASSWORD:
        st.warning("선생님 전용 메뉴입니다. 좌측 사이드바에 올바른 비밀번호를 입력해 주세요.")
    else:
        st.sidebar.success("인증 완료")
        
        teacher_tab1, teacher_tab2 = st.tabs(["📤 새 숙제 등록", "🖨️ 학생별 오답노트 인쇄"])
        
        # --- [탭 1] 새 숙제 등록 (PDF 자동 자르기 및 이미지 직접 업로드 지원) ---
        with teacher_tab1:
            st.markdown("### 📤 새 숙제 문제 등록")
            hw_title = st.text_input("숙제 이름 (예: 3월 2주차 문법 - 음운의 변동)")
            
            upload_mode = st.radio("업로드 방식을 선택하세요", ["📄 PDF 자동 문제 분할 업로드", "🖼️ 이미지 파일 직접 업로드 (1.png, 2.png 등)"])
            
            if upload_mode == "📄 PDF 자동 문제 분할 업로드":
                st.info("💡 **PDF 지원 안내**: 텍스트 선택이 가능한 PDF 문서를 올리면 '1.', '2.' 등 문제 번호 위치를 인식해 이미지로 자동 자릅니다.")
                pdf_file = st.file_uploader("PDF 파일을 선택하세요", type=['pdf'])
                
                if st.button("PDF로 숙제 등록 완료"):
                    if not hw_title.strip():
                        st.error("숙제 이름을 입력해 주세요.")
                    elif not pdf_file:
                        st.error("PDF 파일을 업로드해 주세요.")
                    else:
                        with st.spinner("PDF에서 문제 번호를 읽고 이미지로 자동 분할 중입니다..."):
                            hw_id, q_count = save_homework_from_pdf(hw_title.strip(), pdf_file)
                            if q_count > 0:
                                st.success(f"✅ '{hw_title}' 등록 완료! 총 {q_count}문제가 자동 자르기로 저장되었습니다.")
                            else:
                                st.error("PDF에서 문제 번호(1., 2. 등)를 찾지 못했습니다. 스캔본/이미지형 PDF인 경우 이미지 직접 업로드 방식을 사용해 주세요.")
            
            else:
                st.info("💡 **팁**: 캡처한 이미지 파일명을 1.png, 2.png 순으로 붙여 업로드하세요.")
                uploaded_files = st.file_uploader("문제 이미지 파일들을 선택하세요", type=['png', 'jpg', 'jpeg'], accept_multiple_files=True)
                
                if st.button("이미지로 숙제 등록 완료"):
                    if not hw_title.strip():
                        st.error("숙제 이름을 입력해 주세요.")
                    elif not uploaded_files:
                        st.error("문제 이미지 파일을 선택해 주세요.")
                    else:
                        hw_id = save_homework_from_images(hw_title.strip(), uploaded_files)
                        st.success(f"✅ '{hw_title}' 등록 완료! 총 {len(uploaded_files)}문제가 저장되었습니다.")
        
        # --- [탭 2] 개인별 오답노트 인쇄 ---
        with teacher_tab2:
            st.markdown("### 🖨️ 제출된 학생 오답노트 출력")
            
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
                st.markdown(f"## 📄 맞춤 오답노트: {student_name} 학생")
                st.write(f"**숙제명:** {hw_title} | **제출일:** {submitted_at}")
                st.write(f"**다시 풀어볼 오답 번호:** {wrong_str}번")
                st.markdown("---")
                
                images = get_wrong_questions_images(hw_id, wrong_list)
                
                for q_num, img_bytes in images:
                    st.markdown(f"### 📍 문제 {q_num}번")
                    st.image(img_bytes, use_container_width=True)
                    st.write("📝 **[풀이 / 정답 작성 공간]**")
                    st.write("\n" * 3)
                    st.markdown("---")
                    
                st.markdown("<div class='no-print'><b>💡 팁:</b> 웹브라우저에서 <b>Ctrl + P</b> (Mac은 <b>Cmd + P</b>)를 누르면 옆 메뉴바 없이 오답지 시험지만 깔끔하게 PDF로 저장하거나 인쇄할 수 있습니다.</div>", unsafe_allow_html=True)
