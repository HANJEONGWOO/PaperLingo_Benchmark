# PaperLingo 전체 라벨링 v2

상태: complete / 처리 1,490건 / 전체 1,490건 / 표현 6,188개

`labeling_prompt_v2.md`를 사용하고 Codex CLI에 모델 `gpt-6-astra`, reasoning effort `high`를 지정했다. 기존 ChatGPT 로그인으로 실행했으며 파일럿 결과를 재사용하지 않았다.

논문별 결과는 `abstract_labels_v2.jsonl`, 전체 처리가 완료되면 원본 필드와 expressions를 합친 결과는 `abstract_data_labeled_v2.jsonl`에 저장한다. 원본 순서를 유지한다.

## 표현 수 분포

| 논문당 표현 수 | 논문 수 |
|---:|---:|
| 0 | 12 |
| 1 | 40 |
| 2 | 121 |
| 3 | 165 |
| 4 | 349 |
| 5 | 803 |

## 유형 분포

| 유형 | 표현 수 |
|---|---:|
| preposition_pattern | 1779 |
| collocation | 1300 |
| rhetorical_frame | 871 |
| academic_vocabulary | 742 |
| discourse_marker | 556 |
| verb_pattern | 492 |
| grammar_pattern | 448 |

## 검증과 기록

ID·순서·원문 연속 문자열·JSON 구조·최대 5개·허용 유형·빈 값·중복 표현을 검사했다. 입력 데이터와 프롬프트의 SHA-256을 실행 시작 시점과 대조했다.

`labeling_v2_run/jobs/`에 묶음별 모델 응답, 실행 이벤트, 설정 및 사용량을 보관했다. 실패 시도도 로그에 남으며 완료된 묶음만 최종 출력에 포함한다.

완료 묶음 149개, 기록된 실행 시도 151회, 실패 시도 2회다. 최종 라벨과 저장된 모델 응답의 일치 및 요청한 모델·추론 설정의 일관성도 확인했다.

자동 검토 후보 13건은 `labeling_v2_run/review_flags.csv`에 기록했다. 이 목록은 오류 확정이 아니라 대표 형태의 의미·문법을 확인하기 위한 후보이며 자동으로 라벨을 수정하지 않았다. 자동 검증만으로 교육적 적합성이나 CEFR 수준을 보증할 수 없다.

문자 위치는 `labeling_v2_run/expression_spans.jsonl`에 기록했다. Python 유니코드 문자 기준 0부터 시작하며 end는 포함하지 않는다.
