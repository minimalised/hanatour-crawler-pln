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

SOURCE_SPREADSHEET_ID = os.environ.get("SOURCE_SPREADSHEET_ID")
TARGET_SPREADSHEET_ID = os.environ.get("TARGET_SPREADSHEET_ID")

CONCEPTS = ['A', 'B', 'C', 'D', 'E']
NUMS = [1]
TITLE_COLUMNS = [f"{c}_{n}" for c in CONCEPTS for n in NUMS]  # A_1 ~ E_1 총 5개
BASE_COLUMNS = ["ID", "상품명", "가격", "URL", "이미지URL", "지역", "출발공항"]
COLUMN_ORDER = BASE_COLUMNS + TITLE_COLUMNS

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
# [함수 2] 상위 랭크 분석 기반 LLM 타이틀 생성기 (프롬프트 고도화)
# ==========================================
async def generate_naver_titles_llm(p, semaphore, index):
    await asyncio.sleep(index * 1.2)

    async with semaphore:
        if not p.get('출발공항') or p['출발공항'] == "없음":
            departure = ""
        else:
            departure = f"[{p['출발공항']}출발] "

        # =================================================================
        # [최종 고도화] 상위 랭크 데이터 분석 기반 5개 콘셉트 프롬프트
        # =================================================================
        prompt = f"""
당신은 네이버 쇼핑 검색 최적화(SEO) 및 소비자 클릭률(CTR)을 극대화하는 국내 최고 수준의 퍼포먼스 마케팅 카피라이팅 전문가입니다.
제공된 여행 상품 데이터를 분석하여 네이버 쇼핑 상위 랭크 노출 공식에 맞춘 32자~42자 사이의 상품명 5개를 생성하세요.

[입력 상품 데이터]
- 상품 식별 ID: {p['ID']}
- 원본 상품명: {p['상품명']}
- 여행 지역: {p['지역']}
- 가격: {p['가격']:,}원
- 필수 출발지 문구: "{departure}" (※ 주의: 빈 문구면 절대로 [서울출발], [부산출발] 등을 임의 생성하지 마십시오.)

[🧱 네이버 상위 랭크 5대 콘셉트 규칙]
1. A_1 (SEO 정석형): 메인 검색 키워드 + 핵심 명소/호텔 + 상품유형
   - 어순: {{필수출발지}} {{지역명}} {{일정}} {{핵심명소/호텔}} 패키지 여행
2. B_1 (조건/혜택형): 구매 장벽을 없애주는 소구점 전방 배치
   - 어순: 노쇼핑 노옵션 {{필수출발지}} {{지역명}} {{일정}} {{핵심혜택}} 패키지
3. C_1 (타겟/동반자형): 특정 여행자 그룹(부모님, 가족, 방학) 집중 타깃팅
   - 어순: {{타겟소구(부모님 효도여행/아이동반 가족여행 등)}} {{필수출발지}} {{지역명}} {{일정}} {{맞춤혜택}}
4. D_1 (가성비/실속형): 가격 대비 풍성한 알찬 구성 강조
   - 어순: {{필수출발지}} {{지역명}} {{일정}} 가성비 실속 알찬일정 {{핵심포인트}}
5. E_1 (프리미엄/체험형): 고품격 서비스, 5성급, 특식, 자유시간 등 프리미엄 강조
   - 어순: 직항 프리미엄 {{필수출발지}} {{지역명}} {{일정}} {{VIP혜택/5성급}} 패키지

[📋 퓨샷 예시 (Few-Shot)]
■ 예시 (입력: 출발지="[대구출발] ", 원본="백두산 3박4일 노팁/노옵션/노쇼핑 여행 관광지 연길 천지 홈쇼핑")
{{
  "A_1": "[대구출발] 백두산 3박4일 연길 천지 관광 핵심일정 패키지 여행",
  "B_1": "노쇼핑 노옵션 노팁 [대구출발] 백두산 3박4일 연길 천지 패키지",
  "C_1": "부모님 효도여행 [대구출발] 백두산 3박4일 연길 천지 전문가이드 동행",
  "D_1": "[대구출발] 백두산 3박4일 가성비 실속 알찬일정 천지 관광 포함",
  "E_1": "직항 프리미엄 [대구출발] 백두산 3박4일 5성급 호텔 VIP 천지 투어"
}}

[⚠️ 핵심 가이드라인]
1. 글자 수 제약: 모든 문장은 공백 포함 반드시 32자 이상 ~ 42자 이하로 작성하십시오.
2. 기호 제한: 쉼표(,), 느낌표(!), 해시태그(#) 등 특수문자 전면 금지.
3. 어순 자연스러움: 스팸성 키워드 단순 나열을 금지하고 완벽한 띄어쓰기를 준수하십시오.

반드시 지정된 JSON 규격으로만 응답하세요.
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
            response = await openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that outputs compliant JSON based on the provided schema."},
                    {"role": "user", "content": prompt}
                ],
                response_format=json_schema_format,
                temperature=0.4
            )
            return p['ID'], json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f"❌ LLM 타이틀 생성 중 에러 발생 (ID: {p['ID']}): {e}")
            return p['ID'], {}


# ==========================================
# [함수 3] 메인 크롤러 및 데이터 파이프라인 엔진
# ==========================================
async def run_pipeline():
    gc = get_gspread_client()
    
    if not SOURCE_SPREADSHEET_ID:
        print("❌ SOURCE_SPREADSHEET_ID 환경 변수가 설정되지 않았습니다.")
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

    # 2. 크롤링 엔진 가동
    print("\n🕵️ [2단계] 전수 크롤링 진행 중...")
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
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)

                final_items = await page.query_selector_all(".prod_list_wrap ul.type > li")
                for item in final_items:
                    try:
                        main_info = await item.query_selector(":scope > .inr.right")
                        img_check = await item.query_selector(":scope > .inr.img")
                        if not main_info or not img_check:
                            continue

                        title_el = await main_info.query_selector(".item_title")
                        full_title = (await title_el.inner_text()).strip() if title_el else "제목 없음"

                        price_el = await main_info.query_selector(".price")
                        price_raw = await price_el.inner_text() if price_el else "0"
                        price = int("".join(filter(str.isdigit, price_raw))) if any(c.isdigit() for c in price_raw) else 0

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
                    except Exception:
                        continue
            except Exception as url_err:
                print(f"❌ {task['url']} 접속 및 파싱 패스: {url_err}")
                continue

        await browser.close()

    df_new = pd.DataFrame(crawled_raw_products)
    if df_new.empty:
        print("❌ 수집된 상품이 없어 파이프라인을 종료합니다.")
        return

    df_final = df_new.drop_duplicates(subset=["ID"]).copy()
    for col in TITLE_COLUMNS:
        df_final[col] = ""

    # =================================================================
    # 3. [토큰 절약 핵심] '저장된상품' 시트 연동 및 캐싱 알고리즘
    # =================================================================
    print("\n💰 [3단계] '저장된상품' 캐시 시트 대조 (LLM 토큰 절감 프로세스)...")
    cached_sheet_name = "저장된상품"
    github_sheet_name = "github"
    df_cached = pd.DataFrame()

    if TARGET_SPREADSHEET_ID:
        try:
            target_doc = gc.open_by_key(TARGET_SPREADSHEET_ID)
            
            # '저장된상품' 시트 존재 여부 확인 및 생성
            sheet_list = [s.title for s in target_doc.worksheets()]
            if cached_sheet_name not in sheet_list:
                cached_ws = target_doc.add_worksheet(title=cached_sheet_name, rows=1000, cols=20)
                cached_ws.append_row(COLUMN_ORDER)
                print(f"✨ [{cached_sheet_name}] 시트가 생성되었습니다.")
            else:
                cached_ws = target_doc.worksheet(cached_sheet_name)
                cached_records = cached_ws.get_all_records()
                if cached_records:
                    df_cached = pd.DataFrame(cached_records)

            # 캐시 데이터가 유효하면 ID 매핑을 통해 기존 A_1~E_1 타이틀 복사
            if not df_cached.empty and all(col in df_cached.columns for col in ["ID"] + TITLE_COLUMNS):
                df_cache_map = df_cached[["ID"] + TITLE_COLUMNS].drop_duplicates(subset=["ID"])
                
                # 비어있지 않은 실제 생성된 데이터만 필터링
                valid_cache = df_cache_map[df_cache_map["A_1"].str.strip() != ""]
                
                df_final = pd.merge(
                    df_final.drop(columns=TITLE_COLUMNS, errors='ignore'),
                    valid_cache,
                    on="ID",
                    how="left"
                )
                for col in TITLE_COLUMNS:
                    df_final[col] = df_final[col].fillna("")
                
                saved_count = (df_final["A_1"] != "").sum()
                print(f"💡 [캐시 재활용 성공] 총 {len(df_final)}개 상품 중 {saved_count}개는 '저장된상품' 시트에서 재사용 (토큰 100% 절감!)")
        except Exception as e:
            print(f"⚠️ 저장된상품 시트 연동 중 참조 에러: {e}")

    # LLM 생성이 꼭 필요한 신규 상품만 필터링
    is_need_llm = df_final["A_1"] == ""
    df_need_llm = df_final[is_need_llm].copy()

    print(f"🚀 [최종 연산 대상] 신규 LLM 타이틀 생성 필요 상품: {len(df_need_llm)}개")

    # 4. 신규 상품에 대해서만 LLM 호출
    if len(df_need_llm) > 0:
        records_to_llm = df_need_llm.to_dict(orient="records")
        sem = asyncio.Semaphore(LLM_CONCURRENCY)  
        tasks = [generate_naver_titles_llm(p, sem, idx) for idx, p in enumerate(records_to_llm)]

        print(f"🔗 신규 {len(tasks)}개 상품 LLM 연산 시작...")
        llm_results = await asyncio.gather(*tasks)

        new_llm_rows = []
        for p_id, res in llm_results:
            if not res:
                continue
            matched = df_final[df_final["ID"] == p_id]
            if matched.empty:
                continue
            idx = matched.index[0]
            for col in TITLE_COLUMNS:
                df_final.at[idx, col] = res.get(col, "").strip()

            # 신규로 연산된 Row는 '저장된상품' 캐시 시트에 누적 적재할 준비
            updated_row = df_final.loc[idx].reindex(COLUMN_ORDER, fill_value="").tolist()
            new_llm_rows.append(updated_row)

        # '저장된상품' 시트에 새 연산 결과 추가(Append)
        if TARGET_SPREADSHEET_ID and new_llm_rows:
            try:
                cached_ws = gc.open_by_key(TARGET_SPREADSHEET_ID).worksheet(cached_sheet_name)
                cached_ws.append_rows(new_llm_rows)
                print(f"💾 [캐시 저장 완료] 신규 연산된 {len(new_llm_rows)}개 상품이 [{cached_sheet_name}] 시트에 누적 저장되었습니다.")
            except Exception as e:
                print(f"❌ 캐시 저장 중 에러: {e}")

    # 5. 최종 데이터 'github' 시트 동기화 (현재 살아있는 상품 리스트)
    print(f"\n💾 [5단계] github 출력 시트 업데이트 중...")
    df_final = df_final.reindex(columns=COLUMN_ORDER, fill_value="")
    data_to_upload = [df_final.columns.values.tolist()] + df_final.values.tolist()

    if TARGET_SPREADSHEET_ID:
        try:
            target_doc = gc.open_by_key(TARGET_SPREADSHEET_ID)
            sheet = target_doc.worksheet(github_sheet_name)
            sheet.clear()
            sheet.update(values=data_to_upload, range_name='A1')
            print(f"🚀 [적재 완료] [{github_sheet_name}] 시트 동기화 완료!")
        except Exception as e:
            print(f"❌ github 시트 적재 실패: {e}")

    print(f"\n🎉 모든 파이프라인이 정상 종료되었습니다!")


if __name__ == "__main__":
    asyncio.run(run_pipeline())
