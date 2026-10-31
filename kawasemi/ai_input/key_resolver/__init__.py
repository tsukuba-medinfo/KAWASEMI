"""被験者ID解決(resolver)。

KAWASEMI は名寄せ値を保持も受領もしない（外部解決のみ＝第4の分離面）。

**実装は `FhirIdentifierResolver` の1つである**。以前は Protocol
（`base.KeyResolver`）＋ factory（`settings.key_resolver_backend`）＋ 別プロセス
HTTP 実装（`HttpKeyResolver`）の3層を持っていたが、**切り替えの利用者が
現れなかった**——`http` backend を選ぶ設定は本番・デモ・CI のどこにも無く、
`HttpKeyResolver` は自分のテストからのみ呼ばれていた。名寄せ系を別プロセスへ
物理隔離する要求が実際に出た時点で、その利用者と一緒に Protocol と factory を
書き直す。
"""

from kawasemi.ai_input.key_resolver.fhir_resolver import FhirIdentifierResolver
from kawasemi.ai_input.key_resolver.format import apply_physical_format, mask_value
from kawasemi.ai_input.key_resolver.schema import (
    ResolveContext,
    ResolvedKey,
    ResolveStatus,
)

__all__ = [
    "FhirIdentifierResolver",
    "apply_physical_format",
    "mask_value",
    "ResolveContext",
    "ResolvedKey",
    "ResolveStatus",
]
