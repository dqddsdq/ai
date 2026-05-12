import os
from dotenv import load_dotenv
import requests

# 1. .env 파일 로드 (환경 변수를 시스템 메모리에 주입)
load_dotenv()

# 2. 환경 변수에서 키 읽어오기
openai_key = os.getenv("OPENAI_API_KEY")
google_key = os.getenv("GOOGLE_API_KEY")

print("--- API 환경 변수 로딩 테스트 ---")

if openai_key and google_key:
    print(f"✅ 성공: OpenAI 키 로드됨 (Prefix: {openai_key[:5]}...)")
    print(f"✅ 성공: Google API 키 로드됨 (Prefix: {google_key[:5]}...)")
else:
    print("❌ 실패: 일부 필수 API 키가 .env 파일에 설정되지 않았거나 로드되지 않았습니다.")

# 3. 로드된 키를 사용하여 실제 API 호출 (예시)
# headers = {"Authorization": f"Bearer {openai_key}"}
# response = requests.get("https://api.openai.com/v1/...", headers=headers)
# print("\n[API 호출 시도 완료]")