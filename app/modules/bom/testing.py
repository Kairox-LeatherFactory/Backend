

# import logging

# from app.core.config import settings


# def _configure_logging() -> None:
#     """Configure the root logger ONCE so every module's `logging.getLogger(__name__)`
#     surfaces to stdout (captured by Docker / journald). Level is driven by
#     settings.log_level — set LOG_LEVEL=DEBUG in .env to trace each pipeline step.
#     Runs at import time so even startup logs are formatted consistently."""
#     logging.basicConfig(
#         level=getattr(logging, settings.log_level.upper(), logging.INFO),
#         format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
#         force=True,   # override uvicorn's default handler so our format wins
#     )
#     # asyncpg/sqlalchemy chatter stays at WARNING unless we explicitly want it.
#     logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
# _configure_logging()


# # from app.modules.bom.extraction import extract_order

# # with open("D:\\hamthan-d\\Kaizen AMD\\client\\2-factor\\leather_factory_backend_with_attendance\\backend\\data\\Order-sheet-1.pdf", "rb") as f:
# #     data = f.read()

# # result = extract_order(
# #     data=data,
# #     filename="Order-sheet-1.pdf",
# #     mime="application/pdf"
# # )

# # from pprint import pprint
# # pprint(result)


# from app.modules.bom.extraction import extract_order, extract_spec

# # Read the file as bytes
# with open("D:\\hamthan-d\\Kaizen AMD\\client\\2-factor\\leather_factory_backend_with_attendance\\backend\\data\\spec_sheet_1.xlsx", "rb") as f:
#     data = f.read()

# result = extract_spec(
#     data=data,
#     filename="spec_sheet_1.xlsx",
#     mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
# )

# print(result)

import app.core.config
print(app.core.config.settings.virus_scan_enabled)