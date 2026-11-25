import pytest

from querygate.api_models import QueryRequest
from querygate.errors import GatewayError
from querygate.policy import validate_query


@pytest.mark.parametrize(
    "datasource, sql, params, expected",
    [
        ("finance", "SELECT TOP (10) transaction_id FROM ai_public.v_transactions WHERE amount > ?;", [100], [100]),
        ("finance", "SELECT SUM(amount) FROM ai_public.v_transactions HAVING SUM(amount) > 5", None, []),
        ("finance", "SELECT CAST(transaction_id AS VARCHAR(20)) AS id FROM ai_public.v_transactions WHERE CAST(transaction_id AS VARCHAR(20)) LIKE '1%'", None, []),
        ("finance", "WITH c AS (SELECT transaction_id, amount FROM ai_public.v_transactions) SELECT c.transaction_id, ROW_NUMBER() OVER (ORDER BY c.amount DESC) AS n FROM c JOIN ai_public.v_transactions t ON t.transaction_id = c.transaction_id", None, []),
        ("finance", "SELECT transaction_id FROM [ai_public].[v_transactions] UNION ALL SELECT transaction_id FROM ai_public.v_transactions", {}, []),
        ("finance", "SELECT DATEADD(day, -1, posted_at), ISNULL(amount, 0) FROM ai_public.v_transactions", None, []),
        ("finance", "SELECT 'a; ? -- b' AS literal", None, []),
        ("erp", "SELECT order_id, NVL(total_amount, 0) FROM ai_public.v_orders WHERE total_amount >= :minimum FETCH FIRST 10 ROWS ONLY;", {"minimum": 100}, {"MINIMUM": 100}),
        ("erp", "WITH c AS (SELECT * FROM AI_PUBLIC.V_ORDERS) SELECT ORDER_ID, ROWNUM FROM C WHERE TOTAL_AMOUNT BETWEEN :m AND :M", {"m": 100}, {"M": 100}),
        ("erp", "SELECT o.ORDER_ID FROM \"AI_PUBLIC\".\"V_ORDERS\" o WHERE EXISTS (SELECT 1 FROM AI_PUBLIC.V_ORDERS s WHERE s.ORDER_ID = o.ORDER_ID)", None, {}),
        ("erp", "SELECT ORDER_ID AS ID FROM AI_PUBLIC.V_ORDERS UNION SELECT ORDER_ID FROM AI_PUBLIC.V_ORDERS ORDER BY ID", None, {}),
    ],
)
def test_allowed_queries(settings, datasource, sql, params, expected):
    request = QueryRequest(datasource_id=datasource, sql=sql, params=params)
    query = validate_query(request, settings.datasources[datasource])
    assert query.sql == sql.removesuffix(";")
    assert query.params == expected


@pytest.mark.parametrize(
    "datasource, sql",
    [
        ("finance", "UPDATE ai_public.v_transactions SET amount = 0"),
        ("finance", "SELECT 1; SELECT 2"),
        ("finance", "SELECT transaction_id INTO tmp FROM ai_public.v_transactions"),
        ("finance", "SELECT NEXT VALUE FOR ai_public.seq"),
        ("finance", "SELECT * FROM ai_public.v_transactions WITH (UPDLOCK)"),
        ("finance", "SELECT * FROM another_db.ai_public.v_transactions"),
        ("finance", "SELECT * FROM server_name.db_name.ai_public.v_transactions"),
        ("finance", "SELECT * FROM dbo.private_table"),
        ("finance", "WITH v_transactions AS (SELECT * FROM dbo.private_table) SELECT * FROM v_transactions"),
        ("finance", "SELECT * FROM v_transactions"),
        ("finance", "SELECT dbo.custom_function(transaction_id) FROM ai_public.v_transactions"),
        ("finance", "SELECT 1 /* comment */"),
        ("finance", "SELECT 1 -- comment\n"),
        ("finance", "SELECT 1; -- comment\n"),
        ("erp", "SELECT * FROM AI_PUBLIC.V_ORDERS FOR UPDATE"),
        ("erp", "SELECT * FROM AI_PUBLIC.V_ORDERS@remote"),
        ("erp", "SELECT ai_public.seq.NEXTVAL FROM AI_PUBLIC.V_ORDERS"),
        ("erp", "SELECT custom_function(ORDER_ID) FROM AI_PUBLIC.V_ORDERS"),
        ("erp", "SELECT undeclared_name FROM AI_PUBLIC.V_ORDERS"),
        ("erp", "SELECT AI_PUBLIC.undeclared_name FROM AI_PUBLIC.V_ORDERS"),
        ("erp", "SELECT x.undeclared_name FROM AI_PUBLIC.V_ORDERS x"),
        ("erp", "SELECT x.TOTAL_AMOUNT FROM (SELECT ORDER_ID FROM AI_PUBLIC.V_ORDERS) x"),
        ("erp", 'SELECT * FROM "ai_public"."v_orders"'),
        ("erp", "SELECT /*+ FIRST_ROWS(1) */ * FROM AI_PUBLIC.V_ORDERS"),
    ],
)
def test_policy_denials(settings, datasource, sql):
    with pytest.raises(GatewayError) as error:
        validate_query(QueryRequest(datasource_id=datasource, sql=sql), settings.datasources[datasource])
    assert error.value.code in {"POLICY_DENIED", "SQL_PARSE_ERROR", "OBJECT_NOT_ALLOWED", "COLUMN_NOT_ALLOWED"}


@pytest.mark.parametrize(
    "datasource, sql, params",
    [
        ("finance", "SELECT ?", []),
        ("finance", "SELECT ?", {"value": 10}),
        ("finance", "SELECT 1", [10]),
        ("erp", "SELECT :a FROM AI_PUBLIC.V_ORDERS", {"b": 10}),
        ("erp", "SELECT :a FROM AI_PUBLIC.V_ORDERS", {"a": 10, "A": 20}),
        ("erp", "SELECT :a FROM AI_PUBLIC.V_ORDERS", [10]),
    ],
)
def test_invalid_bindings(settings, datasource, sql, params):
    with pytest.raises(GatewayError, match="bind markers"):
        validate_query(QueryRequest(datasource_id=datasource, sql=sql, params=params), settings.datasources[datasource])
