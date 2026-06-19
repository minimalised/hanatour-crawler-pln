import os
import json
import asyncio
import hashlib
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright
from openai import AsyncOpenAI

# OpenAI 및 구글 시트 기본 설정
openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", "YOUR_LOCAL_API_KEY"))

# [수정] 하드코딩된 스프레드시트 ID를 환경 변수 처리 (없을 경우 기존 ID를 폴백으로 유지하거나 에러 처리 가능)
SOURCE_SPREADSHEET_ID = os.environ.get("SOURCE_SPREADSHEET_ID", "1mH51VHs4y0FgClkUBvZgw7oY3Yv7gQBA_a3um9uhX0I")
TARGET_SPREADSHEET_ID = os.environ.get("TARGET_SPREADSHEET_ID")

# [수정] 총 5개 콘셉트 x 1개씩 = 총 5개 타이틀 마스터 컬럼 정의
CONCEPTS = ['A', 'B', 'C', 'D', 'E']
NUMS = [1]
TITLE_COLUMNS = [f"{c}_{n}" for c in CONCEPTS for n in NUMS]  # A_1 ~ E_1 총 5개
BASE_COLUMNS = ["ID", "상품명", "가격", "URL", "이미지URL", "지역", "출발공항"]
COLUMN_ORDER = BASE_COLUMNS + TITLE_COLUMNS

# 속도 제어를 위해 기본 Concurrency 제한
LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "4"))


# ==========================================
# [함수 1] 구글 시트 연동 인스턴스 생성
# ==========================================
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    json_raw = os.environ.get("GOOGLE_JSON_RAW")
    if json_raw:
        return gspread.authorize(Credentials.from_service_account_info(json.loads(json_raw), scopes=scopes))
    return gspread.authorize(Credentials.from_service_account_file('secrets.json', scopes=scopes))


# ==========================================
# [함수 2] 단일 LLM 타이틀 생성기
# ==========================================
async def generate_naver_titles_llm(p, semaphore, index):
    # 상품 순번(index)당 시차 지연을 주어 TPM 폭발 차단
    await asyncio.sleep(index * 1.5)

    async with semaphore:
        departure = f"[{p['출발공항']}출발]" if p['출발공항'] != "없음" else ""

        price_grade = (
            "세이브" if "[세이브]" in p['상품명'] else
            "스탠다드" if "[스탠다드]" in p['상품명'] else
            "프리미엄" if "[프리미엄]" in p['상품명'] else
            "일반"
        )

        grade_rule = ""
        if price_grade == "세이브":
            grade_rule = "- 등급 소구: 가성비 실속 라인 플랜입니다. '세이브' 단어는 쓰지 말고 [실속형여행], [가성비추천], [합리적선택], [부담없는플랜] 등의 키워드를 카피마다 다채롭게 흩뿌리세요."
        elif price_grade == "스탠다드":
            grade_rule = "- 등급 소구: 표준 스탠다드 라인입니다. '스탠다드' 단어는 쓰지 말고 [핵심일정포함], [완벽구성패키지], [알찬일정여행], [밸런스추천] 등의 키워드를 흩뿌리세요."
        elif price_grade == "프리미엄":
            grade_rule = "- 등급 소구: 하이엔드 고가 라인입니다. '프리미엄' 단어는 쓰지 말고 [노쇼핑노팁], [풀옵션보장], [여유로운자유시간], [전일정5성급호텔숙박] 등의 고급 키워드를 전면에 배치하세요."

        # [수정] 15개 생성에서 5개 생성(각 콘셉트별 1개)으로 프롬프트 가이드 수정
        prompt = f"""
당신은 네이버 쇼핑 검색 최적화(SEO) 및 소비자 클릭률(CTR)을 극대화하는 국내 최고 수준의 퍼포먼스 마케팅 카피라이팅 전문가입니다.
제공된 여행 상품 데이터를 분석하여 차별화된 상품명 5개를 생성하세요.

[입력 상품 데이터]
- 상품 식별 ID: {p['ID']}
- 원본 상품명: {p['상품명']}
- 여행 지역: {p['지역']}
- 가격/금액: {p['가격']:,}원
- 필수 출발지 문구: {departure} (이 문구가 비어있지 않다면 무조건 최종 상품명 가장 맨 앞에 고정 배치할 것)
{grade_rule}

[⚠️ 핵심 제약 가이드라인]
1. 글자 수 제약: 모든 상품명은 공백 포함 최소 32자 ~ 최대 45자 사이로 구성한다. (45자 절대 초과 금지)
2. 문장부호 사용 제한: 최종 생성되는 모든 상품명 내부에는 쉼표(,), 느낌표(!), 물결 (~), 플러스(+) 같은 부호나 특수문자를 절대 포함할 수 없다. 범위는 붙임표 대시 기호(ex: 5-6일)로 치환한다.
3. 정제성: '신상품', '특가' 같은 유치한 홍보성 단어는 전면 배제한다. "주의:", "경고:" 등 시스템 지시어 성격의 텍스트를 삽입하는 것을 절대 금지한다.

[🎯 콘셉트별 마케팅 지향점]
■ 콘셉트 A (정석 SEO형 - 1개): 핵심 지역, 일정, 주요 골프장/호텔 명사 위주의 실용적인 변형 조합.
■ 콘셉트 B (타겟/상황형 - 1개): [부모님극찬휴양], [가족취향저격여행], [부부힐링기념] 등 타겟 심리 자극 단어 배치.
■ 콘셉트 C (혜택/USP형 - 1개): 등급에 맞는 핵심 혜택을 명사화하여 소구. (ex: 반나절자유시간확보, 미슐랭맛집투어 등)
■ 콘셉트 D (감성/트렌디형 - 1개): 요즘뜨는핫플투어, 인생샷명소공략, 감성힐링스팟 등 트렌디한 키워드 결합.
■ 콘셉트 E (기본 대안형 - 1개): 원본 상품명 본연의 가치를 해치지 않는 규격 맞춤형 대안.

반드시 아래 규격의 JSON 오브젝트 포맷으로만 응답하세요. 다른 설명은 전면 금지합니다.
{{
  "A_1": "...",
  "B_1": "...",
  "C_1": "...",
  "D_1": "...",
  "E_1": "..."
}}
"""
        json_schema_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "naver_five_titles_single_schema",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {col: {"type": "string"} for col in TITLE_COLUMNS},
                    "required": TITLE_COLUMNS,
                    "additionalProperties": False
                }
            }
        }

        try:
            await asyncio.sleep(0.3)
            response = await openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that outputs compliant JSON based on the provided schema."},
                    {"role": "user", "content": prompt}
                ],
                response_format=json_schema_format,
                temperature=0.75
            )
            await asyncio.sleep(0.5)
            return p['ID'], json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f"❌ LLM 타이틀 생성 중 에러 발생 (ID: {p['ID']}): {e}")
            return p['ID'], {}


# ==========================================
# [함수 3] 메인 크롤러 및 데이터 파이프라인 엔진
# ==========================================
async def run_pipeline():
    gc = get_gspread_client()
    
    # [수정] 숨김 처리된 타겟 URL 리스트 시트 ID 로드
    if not SOURCE_SPREADSHEET_ID:
        print("❌ SOURCE_SPREADSHEET_ID 환경 변수가 설정되지 않아 파이프라인을 시작할 수 없습니다.")
        return
        
    doc = gc.open_by_key(SOURCE_SPREADSHEET_ID)

    # 1. 구글 스프레드시트에서 타겟 URL 리스트 가져오기
    print("📥 [1단계] 타겟 상품리스트 URL 로드 중...")
    target_rows = doc.worksheet("상품리스트").get_all_values()[1:]
    target_tasks = []
    for r in target_rows:
        if r and r[0].startswith("http"):
            raw_airport = r[2].strip() if len(r) > 2 else ""
            airport_val = raw_airport if raw_airport != "" else "없음"
            target_tasks.append({
                "url": r[0].strip(),
                "region": r[1].strip(),
                "airport": airport_val
            })

    print(f"✅ 총 {len(target_tasks)}개의 크롤링 타겟 URL을 확보했습니다.")

    # 2. 오리지널 코딩의 초고속 가벼운 크롤링 엔진 가동
    print("\n🕵️ [2단계] 전수 크롤링 및 오리지널 스크롤 로딩 시작...")
    crawled_raw_products = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 1024},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        for task in target_tasks:
            try:
                await page.goto(task['url'], wait_until="domcontentloaded", timeout=60000)
                print(f"🔄 로딩 완료: {task['region']} ({task['airport']})")

                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)

                final_items = await page.query_selector_all(".prod_list_wrap ul.type > li")
                for item in final_items:
                    try:
                        main_info = await item.query_selector(":scope > .inr.right")
                        img_check = await item.query_selector(":scope > .inr.img")
                        
                        if not main_info or not img_check:
                            continue

                        # 1. 상품명 추출
                        title_el = await main_info.query_selector(".item_title")
                        full_title = (await title_el.inner_text()).strip() if title_el else "제목 없음"

                        # 2. 가격 추출
                        price_el = await main_info.query_selector(".price")
                        price_raw = await price_el.inner_text() if price_el else "0"
                        price = int("".join(filter(str.isdigit, price_raw))) if any(c.isdigit() for c in price_raw) else 0

                        # 3. 이미지 URL 추출
                        img_el = await img_check.query_selector("img")
                        img_url = ""
                        if img_el:
                            data_src = await img_el.get_attribute("data-src")
                            src = await img_el.get_attribute("src")
                            potential_url = data_src if data_src else src
                            if potential_url and "bg_alpha" not in potential_url:
                                img_url = potential_url.strip()

                        if img_url and img_url.startswith("//"):
                            img_url = "https:" + img_url

                        # 고유 ID 및 데이터 바인딩
                        unique_str = f"{full_title}_{price}_{task['airport']}"
                        product_id = hashlib.md5(unique_str.encode('utf-8')).hexdigest()[:8]

                        crawled_raw_products.append({
                            "ID": product_id,
                            "상품명": full_title,
                            "가격": price,
                            "URL": task['url'],
                            "이미지URL": img_url if img_url else "https://via.placeholder.com/150",
                            "지역": task['region'],
                            "출발공항": task['airport']
                        })
                    except Exception as item_err:
                        print(f"⚠️ 개별 상품 파싱 에러 패스: {item_err}")
                        continue

            except Exception as url_err:
                print(f"❌ {task['url']} 접속 및 파싱 에러 패스: {url_err}")
                continue

        await browser.close()

    df_new = pd.DataFrame(crawled_raw_products)
    print(f"✅ 크롤링 완료: 현재 웹상에 살아있는 상품 총 {len(df_new)}개 수집됨.")

    # 3~5. 데이터 대조 연산
    print("\n📊 [3~5단계] 최신화 연산 진행 (중복 제거 및 마스터 정제)...")
    if df_new.empty:
        print("❌ 수집된 상품이 없어 파이프라인을 종료합니다.")
        return

    df_final = df_new.drop_duplicates(subset=["ID"]).copy()
    for col in TITLE_COLUMNS:
        df_final[col] = ""

    worksheet_name = "github"

    if TARGET_SPREADSHEET_ID:
        try:
            target_doc = gc.open_by_key(TARGET_SPREADSHEET_ID)
            old_records = target_doc.worksheet(worksheet_name).get_all_records()
            if old_records:
                df_old = pd.DataFrame(old_records)
                if all(col in df_old.columns for col in ["ID"] + TITLE_COLUMNS):
                    df_old_titles = df_old[["ID"] + TITLE_COLUMNS].drop_duplicates(subset=["ID"])
                    df_final = pd.merge(
                        df_final.drop(columns=TITLE_COLUMNS, errors='ignore'),
                        df_old_titles,
                        on="ID",
                        how="left"
                    )
                    for col in TITLE_COLUMNS:
                        df_final[col] = df_final[col].fillna("")
                    print("✅ [스마트 증분] 기존 적재된 5대 콘셉트 타이틀 매핑 성공 및 기존 연산 보전 완료.")
        except Exception as e:
            print(f"ℹ️ 기존 적재 시트 대조 패스: {e}")

    is_new_product = df_final["A_1"] == ""
    df_need_llm = df_final[is_new_product].copy()

    print(f"🚀 [안전 흐름 제어 연산] 총 {len(df_final)}개 상품 중 신규 연산 대상 상품: {len(df_need_llm)}개")

    if len(df_need_llm) > 0:
        records_to_llm = df_need_llm.to_dict(orient="records")
        sem = asyncio.Semaphore(LLM_CONCURRENCY)  
        tasks = [generate_naver_titles_llm(p, sem, idx) for idx, p in enumerate(records_to_llm)]

        print(f"🔗 총 {len(tasks)}개의 상품을 시차 분산형 Queue 방식으로 안전하게 동시 처리 시작...")
        llm_results = await asyncio.gather(*tasks)
        print("📥 모든 독립 연산 응답 수신 완료! 데이터프레임 매핑을 시작합니다.")

        for p_id, res in llm_results:
            if not res:
                continue
            matched = df_final[df_final["ID"] == p_id]
            if matched.empty:
                continue
            idx = matched.index[0]
            for col in TITLE_COLUMNS:
                df_final.at[idx, col] = res.get(col, "[Error]").strip()

    # 6. 최종 데이터 적재
    print(f"\n💾 [6단계] 최종 데이터 적재 준비 (총 {len(df_final)}개 상품)...")
    df_final = df_final.reindex(columns=COLUMN_ORDER, fill_value="")
    data_to_upload = [df_final.columns.values.tolist()] + df_final.values.tolist()

    if TARGET_SPREADSHEET_ID:
        try:
            target_doc = gc.open_by_key(TARGET_SPREADSHEET_ID)
            sheet = target_doc.worksheet(worksheet_name)
            sheet.clear()
            sheet.update(values=data_to_upload, range_name='A1')
            print(f"🚀 [적재 완료] Secrets 타겟 시트 [{target_doc.title}] 동기화 성공!")
        except Exception as e:
            print(f"❌ 시트 적재 실패 (ID: {TARGET_SPREADSHEET_ID}): {e}")
    else:
        print("⚠️ [경고] TARGET_SPREADSHEET_ID 환경 변수가 설정되지 않아 구글 시트에 적재하지 못했습니다.")

    print(f"\n🎉 고유 ID 기반 마스터 {len(COLUMN_ORDER)}대 컬럼 데이터 최신화 파이프라인이 정상 종료되었습니다!")


if __name__ == "__main__":
    asyncio.run(run_pipeline())
