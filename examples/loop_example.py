"""Loop example: hybrid (best_of_n → review_guided) on a dummy issue."""

import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swe_review import (  # noqa: E402
    LoopSkill, ReviewSkill, ReviseSkill, GenerateSkill, VerifySkill, ShellTools,
)


async def main() -> None:
    tool = ShellTools()
    review = ReviewSkill(tool_adapter=tool)
    revise = ReviseSkill(tool_adapter=tool)
    generate = GenerateSkill(tool_adapter=tool)
    verify = VerifySkill(repo_path=".")  # 默认 sandbox=True

    loop = LoopSkill(
        review_skill=review,
        revise_skill=revise,
        generator_skill=generate,
        verify_skill=verify,
        strategy="hybrid",
        max_iterations=5,
        n_best_of=3,
    )
    res = await loop.execute(
        issue="NullPointerException when reading user with id=null from user_service.get(id)",
        repo_path=".",
        strategy="hybrid",
    )
    print(json.dumps(res.payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
