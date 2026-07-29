from __future__ import annotations

import copy
import csv
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sqlglot import exp, parse_one

from app.config import PROJECT_ROOT
from app.models import (
    AssetInput,
    ColumnMetadata,
    DatasetSplit,
    EvaluationPair,
    GoldLabel,
    LineageInput,
    TableMetadata,
)


DATA_DIR = PROJECT_ROOT / "data"
ASSETS_PATH = DATA_DIR / "assets.jsonl"
PAIRS_PATH = DATA_DIR / "pairs.csv"
DEMO_CASES_PATH = DATA_DIR / "demo_cases.json"
MANIFEST_PATH = DATA_DIR / "dataset_manifest.json"
SOURCE_SCHEMA_PATH = DATA_DIR / "source_schema" / "fineract_subset.json"

FINERACT_COMMIT = "86edc6b19963e2e73ac5a2ad1926843f6832568f"


SOURCE_TABLES: dict[str, list[tuple[str, str, str]]] = {
    "m_client": [
        ("id", "BIGINT", "客户主键"),
        ("account_no", "VARCHAR", "客户账号"),
        ("office_id", "BIGINT", "所属机构"),
        ("display_name", "VARCHAR", "客户展示名称"),
        ("joined_date", "DATE", "加入日期"),
        ("is_deleted", "BOOLEAN", "删除标志"),
    ],
    "m_office": [
        ("id", "BIGINT", "机构主键"),
        ("parent_id", "BIGINT", "上级机构"),
        ("hierarchy", "VARCHAR", "机构层级路径"),
        ("name", "VARCHAR", "机构名称"),
        ("opening_date", "DATE", "开业日期"),
    ],
    "m_savings_account": [
        ("id", "BIGINT", "存款账户主键"),
        ("account_no", "VARCHAR", "存款账号"),
        ("client_id", "BIGINT", "客户主键"),
        ("product_id", "BIGINT", "产品主键"),
        ("status_enum", "SMALLINT", "账户状态"),
        ("activation_date", "DATE", "激活日期"),
        ("currency_code", "VARCHAR", "币种"),
        ("account_balance_derived", "DECIMAL", "账户余额"),
    ],
    "m_savings_account_transaction": [
        ("id", "BIGINT", "存款交易主键"),
        ("savings_account_id", "BIGINT", "存款账户主键"),
        ("transaction_type_enum", "SMALLINT", "交易类型"),
        ("transaction_date", "DATE", "交易日期"),
        ("amount", "DECIMAL", "交易金额"),
        ("is_reversed", "BOOLEAN", "冲正标志"),
        ("running_balance_derived", "DECIMAL", "交易后余额"),
    ],
    "m_loan": [
        ("id", "BIGINT", "贷款主键"),
        ("client_id", "BIGINT", "客户主键"),
        ("product_id", "BIGINT", "产品主键"),
        ("loan_status_id", "SMALLINT", "贷款状态"),
        ("currency_code", "VARCHAR", "币种"),
        ("principal_amount", "DECIMAL", "贷款本金"),
        ("principal_outstanding_derived", "DECIMAL", "未偿本金"),
        ("interest_outstanding_derived", "DECIMAL", "未偿利息"),
        ("submittedon_date", "DATE", "申请日期"),
        ("disbursedon_date", "DATE", "放款日期"),
    ],
    "m_loan_transaction": [
        ("id", "BIGINT", "贷款交易主键"),
        ("loan_id", "BIGINT", "贷款主键"),
        ("is_reversed", "BOOLEAN", "冲正标志"),
        ("transaction_type_enum", "SMALLINT", "交易类型"),
        ("transaction_date", "DATE", "交易日期"),
        ("amount", "DECIMAL", "交易金额"),
        ("principal_portion_derived", "DECIMAL", "本金部分"),
        ("interest_portion_derived", "DECIMAL", "利息部分"),
    ],
    "acc_gl_account": [
        ("id", "BIGINT", "科目主键"),
        ("name", "VARCHAR", "科目名称"),
        ("gl_code", "VARCHAR", "科目编码"),
        ("disabled", "BOOLEAN", "停用标志"),
        ("classification_enum", "SMALLINT", "科目分类"),
    ],
    "acc_gl_journal_entry": [
        ("id", "BIGINT", "分录主键"),
        ("account_id", "BIGINT", "科目主键"),
        ("office_id", "BIGINT", "机构主键"),
        ("entry_date", "DATE", "入账日期"),
        ("type_enum", "SMALLINT", "借贷方向"),
        ("amount", "DECIMAL", "分录金额"),
        ("reversed", "BOOLEAN", "冲正标志"),
        ("description", "VARCHAR", "分录摘要"),
    ],
}


def c(name: str, cn_name: str, role: str, data_type: str = "STRING") -> ColumnMetadata:
    return ColumnMetadata(
        name=name,
        cn_name=cn_name,
        comment=cn_name,
        role=role,
        data_type=data_type,
    )


@dataclass(frozen=True, slots=True)
class AssetSpec:
    asset_id: str
    asset_name: str
    description: str
    business_domain: str
    declared_grain: tuple[str, ...]
    sql_text: str
    columns: tuple[ColumnMetadata, ...]
    metric_name: str | None = None
    metric_definition: str | None = None

    @property
    def physical_name(self) -> str:
        return self.asset_id.lower()


SPECS: tuple[AssetSpec, ...] = (
    AssetSpec("ODS_CLIENT", "客户源数据", "Fineract客户主数据原样接入", "customer", ("customer",), "SELECT id AS client_id, account_no, office_id, display_name, joined_date, is_deleted FROM m_client", (c("client_id", "客户编号", "key", "BIGINT"), c("account_no", "客户账号", "attribute"), c("office_id", "机构编号", "dimension", "BIGINT"), c("display_name", "客户名称", "attribute"), c("joined_date", "加入日期", "date", "DATE"), c("is_deleted", "删除标志", "flag", "BOOLEAN"))),
    AssetSpec("ODS_OFFICE", "机构源数据", "Fineract机构层级原样接入", "organization", ("office",), "SELECT id AS office_id, parent_id, hierarchy, name AS office_name, opening_date FROM m_office", (c("office_id", "机构编号", "key", "BIGINT"), c("parent_id", "上级机构编号", "dimension", "BIGINT"), c("hierarchy", "机构层级", "attribute"), c("office_name", "机构名称", "attribute"), c("opening_date", "开业日期", "date", "DATE"))),
    AssetSpec("ODS_DEPOSIT_ACCOUNT", "存款账户源数据", "Fineract存款账户原样接入", "deposit", ("deposit_account",), "SELECT id AS deposit_account_id, account_no, client_id, product_id, status_enum, activation_date, currency_code, account_balance_derived FROM m_savings_account", (c("deposit_account_id", "存款账户编号", "key", "BIGINT"), c("account_no", "存款账号", "attribute"), c("client_id", "客户编号", "dimension", "BIGINT"), c("product_id", "存款产品编号", "dimension", "BIGINT"), c("status_enum", "账户状态", "dimension", "SMALLINT"), c("activation_date", "激活日期", "date", "DATE"), c("currency_code", "币种", "dimension"), c("account_balance_derived", "账户余额", "measure", "DECIMAL"))),
    AssetSpec("ODS_DEPOSIT_TRANSACTION", "存款交易源数据", "Fineract存款交易原样接入", "deposit", ("deposit_transaction",), "SELECT id AS deposit_transaction_id, savings_account_id AS deposit_account_id, transaction_type_enum, transaction_date, amount, is_reversed, running_balance_derived FROM m_savings_account_transaction", (c("deposit_transaction_id", "存款交易编号", "key", "BIGINT"), c("deposit_account_id", "存款账户编号", "dimension", "BIGINT"), c("transaction_type_enum", "交易类型", "dimension", "SMALLINT"), c("transaction_date", "交易日期", "date", "DATE"), c("amount", "交易金额", "measure", "DECIMAL"), c("is_reversed", "冲正标志", "flag", "BOOLEAN"), c("running_balance_derived", "交易后余额", "measure", "DECIMAL"))),
    AssetSpec("ODS_LOAN_ACCOUNT", "贷款账户源数据", "Fineract贷款账户原样接入", "loan", ("loan",), "SELECT id AS loan_id, client_id, product_id, loan_status_id, currency_code, principal_amount, principal_outstanding_derived, interest_outstanding_derived, submittedon_date, disbursedon_date FROM m_loan", (c("loan_id", "贷款编号", "key", "BIGINT"), c("client_id", "客户编号", "dimension", "BIGINT"), c("product_id", "贷款产品编号", "dimension", "BIGINT"), c("loan_status_id", "贷款状态", "dimension", "SMALLINT"), c("currency_code", "币种", "dimension"), c("principal_amount", "贷款本金", "measure", "DECIMAL"), c("principal_outstanding_derived", "未偿本金", "measure", "DECIMAL"), c("interest_outstanding_derived", "未偿利息", "measure", "DECIMAL"), c("submittedon_date", "申请日期", "date", "DATE"), c("disbursedon_date", "放款日期", "date", "DATE"))),
    AssetSpec("ODS_LOAN_TRANSACTION", "贷款交易源数据", "Fineract贷款交易原样接入", "loan", ("loan_transaction",), "SELECT id AS loan_transaction_id, loan_id, transaction_type_enum, transaction_date, amount, principal_portion_derived, interest_portion_derived, is_reversed FROM m_loan_transaction", (c("loan_transaction_id", "贷款交易编号", "key", "BIGINT"), c("loan_id", "贷款编号", "dimension", "BIGINT"), c("transaction_type_enum", "交易类型", "dimension", "SMALLINT"), c("transaction_date", "交易日期", "date", "DATE"), c("amount", "交易金额", "measure", "DECIMAL"), c("principal_portion_derived", "本金部分", "measure", "DECIMAL"), c("interest_portion_derived", "利息部分", "measure", "DECIMAL"), c("is_reversed", "冲正标志", "flag", "BOOLEAN"))),
    AssetSpec("ODS_GL_ACCOUNT", "会计科目源数据", "Fineract会计科目原样接入", "accounting", ("gl_account",), "SELECT id AS gl_account_id, name AS gl_account_name, gl_code, classification_enum, disabled FROM acc_gl_account", (c("gl_account_id", "科目编号", "key", "BIGINT"), c("gl_account_name", "科目名称", "attribute"), c("gl_code", "科目编码", "attribute"), c("classification_enum", "科目分类", "dimension", "SMALLINT"), c("disabled", "停用标志", "flag", "BOOLEAN"))),
    AssetSpec("ODS_GL_JOURNAL", "会计分录源数据", "Fineract会计分录原样接入", "accounting", ("journal_entry",), "SELECT id AS journal_entry_id, account_id AS gl_account_id, office_id, entry_date, type_enum, amount, reversed, description FROM acc_gl_journal_entry", (c("journal_entry_id", "分录编号", "key", "BIGINT"), c("gl_account_id", "科目编号", "dimension", "BIGINT"), c("office_id", "机构编号", "dimension", "BIGINT"), c("entry_date", "入账日期", "date", "DATE"), c("type_enum", "借贷方向", "dimension", "SMALLINT"), c("amount", "分录金额", "measure", "DECIMAL"), c("reversed", "冲正标志", "flag", "BOOLEAN"), c("description", "分录摘要", "attribute"))),
    AssetSpec("DWD_CUSTOMER", "客户标准明细", "关联所属机构并过滤删除客户", "customer", ("customer",), "SELECT c.client_id, c.account_no, c.office_id, o.office_name, c.display_name, c.joined_date FROM ods_client c JOIN ods_office o ON c.office_id = o.office_id WHERE c.is_deleted = 0", (c("client_id", "客户编号", "key", "BIGINT"), c("account_no", "客户账号", "attribute"), c("office_id", "机构编号", "dimension", "BIGINT"), c("office_name", "机构名称", "attribute"), c("display_name", "客户名称", "attribute"), c("joined_date", "加入日期", "date", "DATE"))),
    AssetSpec("DWD_DEPOSIT_ACCOUNT", "存款账户标准明细", "关联客户的有效存款账户明细", "deposit", ("deposit_account",), "SELECT a.deposit_account_id, a.client_id, c.office_id, a.product_id, a.status_enum, a.activation_date, a.currency_code, a.account_balance_derived AS current_balance FROM ods_deposit_account a JOIN dwd_customer c ON a.client_id = c.client_id", (c("deposit_account_id", "存款账户编号", "key", "BIGINT"), c("client_id", "客户编号", "dimension", "BIGINT"), c("office_id", "机构编号", "dimension", "BIGINT"), c("product_id", "产品编号", "dimension", "BIGINT"), c("status_enum", "账户状态", "dimension", "SMALLINT"), c("activation_date", "激活日期", "date", "DATE"), c("currency_code", "币种", "dimension"), c("current_balance", "当前余额", "measure", "DECIMAL"))),
    AssetSpec("DWD_DEPOSIT_TRANSACTION", "存款交易标准明细", "过滤冲正并补充客户和币种的存款交易", "deposit", ("deposit_transaction",), "SELECT t.deposit_transaction_id, a.client_id, t.deposit_account_id, t.transaction_type_enum, t.transaction_date, a.currency_code, t.amount, t.running_balance_derived AS running_balance FROM ods_deposit_transaction t JOIN dwd_deposit_account a ON t.deposit_account_id = a.deposit_account_id WHERE t.is_reversed = 0", (c("deposit_transaction_id", "存款交易编号", "key", "BIGINT"), c("client_id", "客户编号", "dimension", "BIGINT"), c("deposit_account_id", "存款账户编号", "dimension", "BIGINT"), c("transaction_type_enum", "交易类型", "dimension", "SMALLINT"), c("transaction_date", "交易日期", "date", "DATE"), c("currency_code", "币种", "dimension"), c("amount", "交易金额", "measure", "DECIMAL"), c("running_balance", "交易后余额", "measure", "DECIMAL"))),
    AssetSpec("DWD_LOAN_ACCOUNT", "贷款账户标准明细", "关联客户并计算贷款未偿总额", "loan", ("loan",), "SELECT l.loan_id, l.client_id, c.office_id, l.product_id, l.loan_status_id, l.currency_code, l.principal_amount, l.principal_outstanding_derived, l.interest_outstanding_derived, l.principal_outstanding_derived + l.interest_outstanding_derived AS total_outstanding, l.submittedon_date, l.disbursedon_date FROM ods_loan_account l JOIN dwd_customer c ON l.client_id = c.client_id", (c("loan_id", "贷款编号", "key", "BIGINT"), c("client_id", "客户编号", "dimension", "BIGINT"), c("office_id", "机构编号", "dimension", "BIGINT"), c("product_id", "产品编号", "dimension", "BIGINT"), c("loan_status_id", "贷款状态", "dimension", "SMALLINT"), c("currency_code", "币种", "dimension"), c("principal_amount", "贷款本金", "measure", "DECIMAL"), c("principal_outstanding_derived", "未偿本金", "measure", "DECIMAL"), c("interest_outstanding_derived", "未偿利息", "measure", "DECIMAL"), c("total_outstanding", "未偿总额", "measure", "DECIMAL"), c("submittedon_date", "申请日期", "date", "DATE"), c("disbursedon_date", "放款日期", "date", "DATE"))),
    AssetSpec("DWD_LOAN_TRANSACTION", "贷款交易标准明细", "过滤冲正并补充客户和币种的贷款交易", "loan", ("loan_transaction",), "SELECT t.loan_transaction_id, a.client_id, t.loan_id, t.transaction_type_enum, t.transaction_date, a.currency_code, t.amount, t.principal_portion_derived, t.interest_portion_derived FROM ods_loan_transaction t JOIN dwd_loan_account a ON t.loan_id = a.loan_id WHERE t.is_reversed = 0", (c("loan_transaction_id", "贷款交易编号", "key", "BIGINT"), c("client_id", "客户编号", "dimension", "BIGINT"), c("loan_id", "贷款编号", "dimension", "BIGINT"), c("transaction_type_enum", "交易类型", "dimension", "SMALLINT"), c("transaction_date", "交易日期", "date", "DATE"), c("currency_code", "币种", "dimension"), c("amount", "交易金额", "measure", "DECIMAL"), c("principal_portion_derived", "本金部分", "measure", "DECIMAL"), c("interest_portion_derived", "利息部分", "measure", "DECIMAL"))),
    AssetSpec("DWD_GL_JOURNAL", "会计分录标准明细", "过滤冲正和停用科目的标准会计分录", "accounting", ("journal_entry",), "SELECT j.journal_entry_id, j.office_id, j.gl_account_id, a.gl_code, a.gl_account_name, j.entry_date, j.type_enum, j.amount, CASE WHEN j.type_enum = 1 THEN j.amount ELSE -j.amount END AS signed_amount, j.description FROM ods_gl_journal j JOIN ods_gl_account a ON j.gl_account_id = a.gl_account_id WHERE j.reversed = 0 AND a.disabled = 0", (c("journal_entry_id", "分录编号", "key", "BIGINT"), c("office_id", "机构编号", "dimension", "BIGINT"), c("gl_account_id", "科目编号", "dimension", "BIGINT"), c("gl_code", "科目编码", "dimension"), c("gl_account_name", "科目名称", "attribute"), c("entry_date", "入账日期", "date", "DATE"), c("type_enum", "借贷方向", "dimension", "SMALLINT"), c("amount", "分录金额", "measure", "DECIMAL"), c("signed_amount", "带方向金额", "measure", "DECIMAL"), c("description", "分录摘要", "attribute"))),
    AssetSpec("DWS_CUSTOMER_DAILY_DEPOSIT_BALANCE", "客户日存款余额汇总", "取账户每日最后一笔交易后余额并按客户汇总", "deposit", ("customer", "day", "currency"), "WITH ranked AS (SELECT client_id, deposit_account_id, transaction_date, currency_code, running_balance, ROW_NUMBER() OVER (PARTITION BY deposit_account_id, transaction_date ORDER BY deposit_transaction_id DESC) AS rn FROM dwd_deposit_transaction) SELECT client_id, transaction_date AS balance_date, currency_code, SUM(running_balance) AS daily_balance, COUNT(DISTINCT deposit_account_id) AS account_count FROM ranked WHERE rn = 1 GROUP BY client_id, transaction_date, currency_code", (c("client_id", "客户编号", "dimension", "BIGINT"), c("balance_date", "余额日期", "date", "DATE"), c("currency_code", "币种", "dimension"), c("daily_balance", "客户日余额", "measure", "DECIMAL"), c("account_count", "账户数量", "measure", "BIGINT")), "客户日存款余额", "客户各账户当日最后交易后余额之和"),
    AssetSpec("DWS_CUSTOMER_MONTH_DEPOSIT_FLOW", "客户月存款交易汇总", "按客户、月份和币种汇总存款交易金额", "deposit", ("customer", "month", "currency"), "SELECT client_id, DATE_TRUNC('month', transaction_date) AS stat_month, currency_code, SUM(amount) AS transaction_amount, COUNT(DISTINCT deposit_transaction_id) AS transaction_count FROM dwd_deposit_transaction GROUP BY client_id, DATE_TRUNC('month', transaction_date), currency_code", (c("client_id", "客户编号", "dimension", "BIGINT"), c("stat_month", "统计月份", "date", "DATE"), c("currency_code", "币种", "dimension"), c("transaction_amount", "交易金额合计", "measure", "DECIMAL"), c("transaction_count", "交易笔数", "measure", "BIGINT")), "客户月存款交易额", "客户当月存款交易金额合计"),
    AssetSpec("DWS_CUSTOMER_LOAN_OUTSTANDING", "客户贷款未偿汇总", "按客户和币种汇总当前贷款未偿本金与利息", "loan", ("customer", "currency"), "SELECT client_id, currency_code, SUM(principal_outstanding_derived) AS principal_outstanding, SUM(interest_outstanding_derived) AS interest_outstanding, SUM(total_outstanding) AS total_outstanding, COUNT(DISTINCT loan_id) AS loan_count FROM dwd_loan_account GROUP BY client_id, currency_code", (c("client_id", "客户编号", "dimension", "BIGINT"), c("currency_code", "币种", "dimension"), c("principal_outstanding", "未偿本金", "measure", "DECIMAL"), c("interest_outstanding", "未偿利息", "measure", "DECIMAL"), c("total_outstanding", "未偿总额", "measure", "DECIMAL"), c("loan_count", "贷款笔数", "measure", "BIGINT")), "客户贷款未偿余额", "客户当前未偿本金和利息合计"),
    AssetSpec("DWS_CUSTOMER_MONTH_LOAN_PAYMENT", "客户月贷款还款汇总", "按客户、月份和币种汇总贷款交易", "loan", ("customer", "month", "currency"), "SELECT client_id, DATE_TRUNC('month', transaction_date) AS stat_month, currency_code, SUM(amount) AS payment_amount, SUM(principal_portion_derived) AS principal_payment, SUM(interest_portion_derived) AS interest_payment, COUNT(DISTINCT loan_transaction_id) AS transaction_count FROM dwd_loan_transaction GROUP BY client_id, DATE_TRUNC('month', transaction_date), currency_code", (c("client_id", "客户编号", "dimension", "BIGINT"), c("stat_month", "统计月份", "date", "DATE"), c("currency_code", "币种", "dimension"), c("payment_amount", "还款金额", "measure", "DECIMAL"), c("principal_payment", "本金还款", "measure", "DECIMAL"), c("interest_payment", "利息还款", "measure", "DECIMAL"), c("transaction_count", "交易笔数", "measure", "BIGINT")), "客户月贷款还款额", "客户当月贷款交易金额合计"),
    AssetSpec("DWS_OFFICE_DAILY_GL", "机构日会计余额汇总", "按机构、日期和会计科目汇总带方向金额", "accounting", ("office", "day", "gl_account"), "SELECT office_id, entry_date, gl_account_id, gl_code, SUM(signed_amount) AS net_amount, COUNT(DISTINCT journal_entry_id) AS entry_count FROM dwd_gl_journal GROUP BY office_id, entry_date, gl_account_id, gl_code", (c("office_id", "机构编号", "dimension", "BIGINT"), c("entry_date", "入账日期", "date", "DATE"), c("gl_account_id", "科目编号", "dimension", "BIGINT"), c("gl_code", "科目编码", "dimension"), c("net_amount", "净额", "measure", "DECIMAL"), c("entry_count", "分录笔数", "measure", "BIGINT")), "机构日会计净额", "机构科目每日借贷方向净额"),
    AssetSpec("ADS_CUSTOMER_MONTH_AVG_DEPOSIT", "客户月平均存款余额", "按客户、月份和币种计算有效观察日平均存款余额", "deposit", ("customer", "month", "currency"), "SELECT client_id, DATE_TRUNC('month', balance_date) AS stat_month, currency_code, SUM(daily_balance) / COUNT(DISTINCT balance_date) AS avg_balance FROM dws_customer_daily_deposit_balance GROUP BY client_id, DATE_TRUNC('month', balance_date), currency_code", (c("client_id", "客户编号", "dimension", "BIGINT"), c("stat_month", "统计月份", "date", "DATE"), c("currency_code", "币种", "dimension"), c("avg_balance", "月平均存款余额", "measure", "DECIMAL")), "客户月平均存款余额", "客户每日存款余额合计除以有效观察日数"),
    AssetSpec("ADS_CUSTOMER_MONTH_DEPOSIT_FLOW", "客户月存款交易指标", "面向经营分析的客户月存款交易金额和笔数", "deposit", ("customer", "month", "currency"), "SELECT client_id, stat_month, currency_code, transaction_amount, transaction_count FROM dws_customer_month_deposit_flow", (c("client_id", "客户编号", "dimension", "BIGINT"), c("stat_month", "统计月份", "date", "DATE"), c("currency_code", "币种", "dimension"), c("transaction_amount", "月交易金额", "measure", "DECIMAL"), c("transaction_count", "月交易笔数", "measure", "BIGINT")), "客户月存款交易额", "客户当月存款交易金额和笔数"),
    AssetSpec("ADS_CUSTOMER_LOAN_OUTSTANDING", "客户贷款未偿余额指标", "筛选存在未偿贷款的客户余额", "loan", ("customer", "currency"), "SELECT client_id, currency_code, principal_outstanding, interest_outstanding, total_outstanding, loan_count FROM dws_customer_loan_outstanding WHERE total_outstanding > 0", (c("client_id", "客户编号", "dimension", "BIGINT"), c("currency_code", "币种", "dimension"), c("principal_outstanding", "未偿本金", "measure", "DECIMAL"), c("interest_outstanding", "未偿利息", "measure", "DECIMAL"), c("total_outstanding", "贷款未偿总额", "measure", "DECIMAL"), c("loan_count", "贷款笔数", "measure", "BIGINT")), "客户贷款未偿余额", "未偿本金与未偿利息合计大于零"),
    AssetSpec("ADS_CUSTOMER_MONTH_LOAN_REPAYMENT", "客户月贷款还款指标", "面向经营分析的客户月贷款还款金额", "loan", ("customer", "month", "currency"), "SELECT client_id, stat_month, currency_code, payment_amount, principal_payment, interest_payment, transaction_count FROM dws_customer_month_loan_payment", (c("client_id", "客户编号", "dimension", "BIGINT"), c("stat_month", "统计月份", "date", "DATE"), c("currency_code", "币种", "dimension"), c("payment_amount", "月还款金额", "measure", "DECIMAL"), c("principal_payment", "月本金还款", "measure", "DECIMAL"), c("interest_payment", "月利息还款", "measure", "DECIMAL"), c("transaction_count", "还款笔数", "measure", "BIGINT")), "客户月贷款还款额", "客户当月贷款交易金额及本金利息构成"),
    AssetSpec("ADS_OFFICE_DAILY_GL_BALANCE", "机构日会计净额指标", "面向财务核算的机构科目日净额", "accounting", ("office", "day", "gl_account"), "SELECT office_id, entry_date, gl_account_id, gl_code, net_amount, entry_count FROM dws_office_daily_gl WHERE net_amount <> 0", (c("office_id", "机构编号", "dimension", "BIGINT"), c("entry_date", "入账日期", "date", "DATE"), c("gl_account_id", "科目编号", "dimension", "BIGINT"), c("gl_code", "科目编码", "dimension"), c("net_amount", "科目日净额", "measure", "DECIMAL"), c("entry_count", "分录笔数", "measure", "BIGINT")), "机构日会计净额", "机构会计科目每日非零净额"),
)


EVALUATION_BASE_IDS = [
    "ODS_CLIENT", "ODS_DEPOSIT_ACCOUNT", "ODS_DEPOSIT_TRANSACTION", "ODS_LOAN_ACCOUNT",
    "DWD_CUSTOMER", "DWD_DEPOSIT_ACCOUNT", "DWD_DEPOSIT_TRANSACTION", "DWD_LOAN_ACCOUNT",
    "DWD_LOAN_TRANSACTION", "DWD_GL_JOURNAL", "DWS_CUSTOMER_DAILY_DEPOSIT_BALANCE",
    "DWS_CUSTOMER_MONTH_DEPOSIT_FLOW", "DWS_CUSTOMER_LOAN_OUTSTANDING",
    "DWS_CUSTOMER_MONTH_LOAN_PAYMENT", "DWS_OFFICE_DAILY_GL",
    "ADS_CUSTOMER_MONTH_AVG_DEPOSIT", "ADS_CUSTOMER_MONTH_DEPOSIT_FLOW",
    "ADS_CUSTOMER_LOAN_OUTSTANDING", "ADS_CUSTOMER_MONTH_LOAN_REPAYMENT",
    "ADS_OFFICE_DAILY_GL_BALANCE",
]

TEST_BASE_IDS = {
    "ODS_DEPOSIT_TRANSACTION",
    "DWD_LOAN_ACCOUNT",
    "DWD_GL_JOURNAL",
    "DWS_CUSTOMER_MONTH_DEPOSIT_FLOW",
    "ADS_CUSTOMER_LOAN_OUTSTANDING",
    "ADS_OFFICE_DAILY_GL_BALANCE",
}


def _input_tables(sql: str) -> list[str]:
    root = parse_one(sql, read="spark")
    ctes = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    return sorted({table.name.lower() for table in root.find_all(exp.Table) if table.name.lower() not in ctes})


def _column_signatures(sql: str) -> list[str]:
    root = parse_one(sql, read="spark")
    outer = root if isinstance(root, exp.Select) else root.find(exp.Select)
    if outer is None:
        return []
    signatures: list[str] = []
    for expression in outer.expressions:
        output = expression.alias_or_name
        inputs = sorted(
            {
                f"{column.table}.{column.name}" if column.table else column.name
                for column in expression.find_all(exp.Column)
            }
        )
        if output:
            signatures.append(f"{output.lower()}<-{'|'.join(inputs).lower()}")
    return signatures


def build_base_assets() -> tuple[list[AssetInput], dict[str, list[str]]]:
    assets: list[AssetInput] = []
    physical_roots: dict[str, list[str]] = {}
    known_tables = set(SOURCE_TABLES)
    for spec in SPECS:
        direct = _input_tables(spec.sql_text)
        unknown = sorted(set(direct) - known_tables)
        if unknown:
            raise ValueError(f"{spec.asset_id} references unknown tables: {unknown}")
        roots: set[str] = set()
        for upstream in direct:
            if upstream in SOURCE_TABLES:
                roots.add(upstream)
            else:
                roots.update(physical_roots[upstream])
        asset = AssetInput(
            asset_id=spec.asset_id,
            asset_name=spec.asset_name,
            description=spec.description,
            business_domain=spec.business_domain,
            sql_text=spec.sql_text,
            sql_dialect="spark",
            declared_grain=list(spec.declared_grain),
            metric_name=spec.metric_name,
            metric_definition=spec.metric_definition,
            tables=[
                TableMetadata(
                    name=spec.physical_name,
                    cn_name=spec.asset_name,
                    comment=spec.description,
                    columns=list(spec.columns),
                )
            ],
            lineage=LineageInput(
                direct_upstreams=direct,
                root_sources=sorted(roots),
                column_signatures=_column_signatures(spec.sql_text),
                coverage=0.95,
                freshness=1.0,
                source_reliability=0.90,
            ),
        )
        assets.append(asset)
        physical_roots[spec.physical_name] = sorted(roots)
        known_tables.add(spec.physical_name)
    return assets, physical_roots


def _rename_semantically(name: str) -> str:
    replacements = (
        ("个人客户", "自然人客户"),
        ("客户", "客户对象"),
        ("存款", "储蓄"),
        ("贷款", "信贷"),
        ("交易", "流水"),
        ("会计", "财务核算"),
        ("机构", "营业机构"),
        ("余额", "结余"),
        ("汇总", "统计"),
    )
    changed = name
    for source, target in replacements:
        if source in changed:
            changed = changed.replace(source, target, 1)
            break
    return changed if changed != name else f"{name}等价版本"


def _duplicate_variant(base: AssetInput, index: int) -> AssetInput:
    payload = copy.deepcopy(base.model_dump(mode="python"))
    payload["asset_id"] = f"{base.asset_id}_DUP"
    payload["asset_name"] = _rename_semantically(base.asset_name)
    payload["description"] = _rename_semantically(base.description)
    if index % 3 == 0:
        payload["sql_text"] = parse_one(base.sql_text, read="spark").sql(dialect="spark", pretty=True)
    elif index % 3 == 1:
        payload["sql_text"] = f"WITH source_asset AS ({base.sql_text}) SELECT * FROM source_asset"
    else:
        payload["sql_text"] = f"SELECT * FROM ({base.sql_text}) source_asset WHERE 1 = 1"
    return AssetInput.model_validate(payload)


def _similar_variant(base: AssetInput, index: int, suffix: str = "SIM") -> tuple[AssetInput, str]:
    payload = copy.deepcopy(base.model_dump(mode="python"))
    payload["asset_id"] = f"{base.asset_id}_{suffix}"
    payload["asset_name"] = f"{base.asset_name}不同口径"
    sql = base.sql_text
    if re.search(r"\bSUM\s*\(", sql, re.I) and index % 2 == 0:
        payload["sql_text"] = re.sub(r"\bSUM\s*\(", "AVG(", sql, count=1, flags=re.I)
        payload["metric_definition"] = "首个SUM聚合改为AVG，计算公式与原资产不同"
        scenario = "SIMILAR_AGGREGATION_CONFLICT"
    elif "DATE_TRUNC('month'" in sql.upper().replace('"', "'") or "date_trunc('month'" in sql.lower():
        payload["sql_text"] = re.sub(
            r"DATE_TRUNC\s*\(\s*'month'",
            "DATE_TRUNC('day'",
            sql,
            flags=re.I,
        )
        payload["declared_grain"] = ["day" if item == "month" else item for item in base.declared_grain]
        payload["metric_definition"] = "按日统计，与原月粒度不同"
        scenario = "SIMILAR_TIME_GRAIN_CONFLICT"
    elif " = 0" in sql:
        payload["sql_text"] = sql.replace(" = 0", " = 1", 1)
        payload["metric_definition"] = "核心状态过滤值不同"
        scenario = "SIMILAR_FILTER_CONFLICT"
    else:
        payload["sql_text"] = f"SELECT * FROM ({sql}) scope_variant WHERE 1 = 0"
        payload["metric_definition"] = "额外增加空结果范围过滤，与原资产统计范围不同"
        scenario = "SIMILAR_SCOPE_CONFLICT"
    parse_one(payload["sql_text"], read="spark")
    return AssetInput.model_validate(payload), scenario


def build_pairs(base_assets: list[AssetInput]) -> tuple[list[AssetInput], list[EvaluationPair]]:
    by_id = {asset.asset_id: asset for asset in base_assets}
    selected = [by_id[asset_id] for asset_id in EVALUATION_BASE_IDS]
    split_by_base = {
        asset.asset_id: DatasetSplit.TEST if asset.asset_id in TEST_BASE_IDS else DatasetSplit.DEV
        for asset in selected
    }
    variants: list[AssetInput] = []
    pairs: list[EvaluationPair] = []

    def add_pair(base: AssetInput, other: AssetInput, label: GoldLabel, scenario: str) -> None:
        pairs.append(
            EvaluationPair(
                pair_id=f"PAIR-{len(pairs) + 1:03d}",
                family_id=f"FAMILY-{base.asset_id}",
                asset_a_id=base.asset_id,
                asset_b_id=other.asset_id,
                label=label,
                scenario=scenario,
                split=split_by_base[base.asset_id],
            )
        )

    for index, base in enumerate(selected):
        variant = _duplicate_variant(base, index)
        variants.append(variant)
        add_pair(base, variant, GoldLabel.DUPLICATE, "DUPLICATE_EQUIVALENT_REWRITE")

    dev_bases = [asset for asset in selected if split_by_base[asset.asset_id] == DatasetSplit.DEV]
    test_bases = [asset for asset in selected if split_by_base[asset.asset_id] == DatasetSplit.TEST]
    for index, base in enumerate(dev_bases[:13]):
        variant, scenario = _similar_variant(base, index)
        variants.append(variant)
        add_pair(base, variant, GoldLabel.NOT_DUPLICATE, scenario)
    for index, base in enumerate(test_bases):
        variant, scenario = _similar_variant(base, index + 20)
        variants.append(variant)
        add_pair(base, variant, GoldLabel.NOT_DUPLICATE, scenario)
    extra_variant, extra_scenario = _similar_variant(test_bases[0], 99, suffix="SIM2")
    variants.append(extra_variant)
    add_pair(test_bases[0], extra_variant, GoldLabel.NOT_DUPLICATE, extra_scenario)

    def negative_target(base: AssetInput, pool: list[AssetInput], offset: int) -> AssetInput:
        candidates = [item for item in pool if item.business_domain != base.business_domain]
        if not candidates:
            raise ValueError(f"no cross-domain negative candidate for {base.asset_id}")
        return candidates[offset % len(candidates)]

    for index, base in enumerate(dev_bases[:13]):
        add_pair(
            base,
            negative_target(base, dev_bases, index + 1),
            GoldLabel.NOT_DUPLICATE,
            "CLEAR_CROSS_DOMAIN",
        )
    for index, base in enumerate(test_bases):
        add_pair(
            base,
            negative_target(base, test_bases, index + 1),
            GoldLabel.NOT_DUPLICATE,
            "CLEAR_CROSS_DOMAIN",
        )
    add_pair(
        test_bases[1],
        negative_target(test_bases[1], test_bases, 4),
        GoldLabel.NOT_DUPLICATE,
        "CLEAR_CROSS_DOMAIN_SECOND",
    )
    return [*base_assets, *variants], pairs


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate() -> dict[str, object]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SOURCE_SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    base_assets, _ = build_base_assets()
    all_assets, pairs = build_pairs(base_assets)
    if len(base_assets) != 24 or len(pairs) != 60:
        raise ValueError("dataset cardinality does not match reviewed design")
    if len({asset.asset_id for asset in all_assets}) != len(all_assets):
        raise ValueError("duplicate asset_id generated")

    ASSETS_PATH.write_text(
        "\n".join(asset.model_dump_json() for asset in all_assets) + "\n",
        encoding="utf-8",
    )
    with PAIRS_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["pair_id", "family_id", "asset_a_id", "asset_b_id", "label", "scenario", "split"],
        )
        writer.writeheader()
        for pair in pairs:
            writer.writerow(pair.model_dump(mode="json"))

    categories = {
        "duplicate": [pair for pair in pairs if pair.label == GoldLabel.DUPLICATE],
        "similar_not_duplicate": [pair for pair in pairs if pair.scenario.startswith("SIMILAR_")],
        "clear_not_duplicate": [pair for pair in pairs if pair.scenario.startswith("CLEAR_")],
    }
    demo_pairs = [
        *categories["duplicate"][:4],
        *categories["similar_not_duplicate"][:4],
        *categories["clear_not_duplicate"][:4],
    ]
    asset_names = {asset.asset_id: asset.asset_name for asset in all_assets}
    DEMO_CASES_PATH.write_text(
        json.dumps(
            [
                {
                    "pair_id": pair.pair_id,
                    "title": pair.scenario,
                    "asset_a_id": pair.asset_a_id,
                    "asset_a_name": asset_names[pair.asset_a_id],
                    "asset_b_id": pair.asset_b_id,
                    "asset_b_name": asset_names[pair.asset_b_id],
                    "expected_label": pair.label.value,
                }
                for pair in demo_pairs
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    SOURCE_SCHEMA_PATH.write_text(
        json.dumps(
            {
                "source": "apache/fineract",
                "commit": FINERACT_COMMIT,
                "usage": "public schema only; no customer or transaction rows",
                "tables": {
                    name: [
                        {"name": column, "data_type": data_type, "comment": comment}
                        for column, data_type, comment in columns
                    ]
                    for name, columns in SOURCE_TABLES.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    split_counts = Counter(pair.split.value for pair in pairs)
    scenario_categories = {key: len(value) for key, value in categories.items()}
    manifest: dict[str, object] = {
        "version": "task1_v1-dataset-v1.0",
        "source": "Apache Fineract public schema plus controlled semi-synthetic assets",
        "fineract_commit": FINERACT_COMMIT,
        "base_asset_count": len(base_assets),
        "variant_asset_count": len(all_assets) - len(base_assets),
        "asset_count": len(all_assets),
        "pair_count": len(pairs),
        "split_distribution": dict(sorted(split_counts.items())),
        "scenario_category_distribution": scenario_categories,
        "demo_case_count": len(demo_pairs),
        "data_boundary": "metadata, SQL and lineage only; no real bank record data",
        "assets_sha256": _sha256(ASSETS_PATH),
        "pairs_sha256": _sha256(PAIRS_PATH),
        "demo_cases_sha256": _sha256(DEMO_CASES_PATH),
        "source_schema_sha256": _sha256(SOURCE_SCHEMA_PATH),
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    print(json.dumps(generate(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
