-- POC deployment marker - verifies deploy-dbt reaches the environment's Composer bucket.
-- Expected destination: gs://<COMPOSER_BUCKET>/dbt/
select
    "test" as test_column,
    "value" as value_column

