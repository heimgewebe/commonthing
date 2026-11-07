//! Disposable PostgreSQL proof for voluntary circle admission and revocation.
mod support;

use sqlx::{postgres::PgPoolOptions, Row};

#[tokio::test]
#[ignore = "requires DATABASE_URL for a disposable direct PostgreSQL database"]
async fn circle_member_access_requires_confirmation_and_active_account() {
    let url = support::postgres_proof::validated_direct_disposable_url(
        std::env::var("DATABASE_URL").expect("disposable PostgreSQL URL required"),
    );
    let pool = PgPoolOptions::new()
        .max_connections(3)
        .connect(&url)
        .await
        .unwrap();
    sqlx::migrate!("./migrations").run(&pool).await.unwrap();

    // Same address/position intentionally confers no rights.
    let test_id = uuid::Uuid::new_v4();
    let a = format!("proof-a-{test_id}");
    let b = format!("proof-b-{test_id}");
    let c = format!("proof-c-{test_id}");
    for actor in [&a, &b, &c] {
        sqlx::query(
            "INSERT INTO domain_accounts (id, title, kind, location_lat, location_lon)
                     VALUES ($1, $1, 'garnrolle', 53.555, 10.055)",
        )
        .bind(actor)
        .execute(&pool)
        .await
        .unwrap();
    }
    let circle = uuid::Uuid::new_v4();
    sqlx::query(
        "INSERT INTO domain_audience_circles (id, kind, name, created_by_account_id)
         VALUES ($1, 'household', 'Proof house', $2)",
    )
    .bind(circle)
    .bind(&a)
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO domain_audience_circle_members
            (circle_id, account_id, status, invited_by_account_id, joined_at)
         VALUES ($1, $2, 'active', $2, NOW()), ($1, $3, 'invited', $2, NULL)",
    )
    .bind(circle)
    .bind(&a)
    .bind(&b)
    .execute(&pool)
    .await
    .unwrap();

    async fn can_read(pool: &sqlx::PgPool, circle: uuid::Uuid, actor: &str) -> bool {
        sqlx::query("SELECT weltgewebe_active_circle_member($1, $2) AS allowed")
            .bind(circle)
            .bind(actor)
            .fetch_one(pool)
            .await
            .unwrap()
            .get::<bool, _>("allowed")
    }

    assert!(
        !can_read(&pool, circle, &a).await,
        "forming circle must not grant read"
    );
    assert!(
        !can_read(&pool, circle, &b).await,
        "pending invite must not grant read"
    );
    assert!(
        !can_read(&pool, circle, &c).await,
        "co-located stranger must not read"
    );

    sqlx::query(
        "UPDATE domain_audience_circle_members
                 SET status='active', joined_at=NOW() WHERE circle_id=$1 AND account_id=$2",
    )
    .bind(circle)
    .bind(&b)
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query("UPDATE domain_audience_circles SET state='active' WHERE id=$1")
        .bind(circle)
        .execute(&pool)
        .await
        .unwrap();

    assert!(can_read(&pool, circle, &a).await);
    assert!(can_read(&pool, circle, &b).await);
    assert!(!can_read(&pool, circle, &c).await);

    sqlx::query("UPDATE domain_accounts SET location_lat=NULL, location_lon=NULL WHERE id=$1")
        .bind(&a)
        .execute(&pool)
        .await
        .unwrap();
    assert!(
        can_read(&pool, circle, &a).await,
        "relocation must preserve member rights"
    );

    sqlx::query("DELETE FROM domain_audience_circle_members WHERE circle_id=$1 AND account_id=$2")
        .bind(circle)
        .bind(&b)
        .execute(&pool)
        .await
        .unwrap();
    assert!(
        !can_read(&pool, circle, &b).await,
        "departure must revoke immediately"
    );

    sqlx::query(
        "INSERT INTO domain_audience_circle_members
                 (circle_id, account_id, status, invited_by_account_id)
                 VALUES ($1, $2, 'invited', $3)",
    )
    .bind(circle)
    .bind(&c)
    .bind(&a)
    .execute(&pool)
    .await
    .unwrap();
    assert!(
        !can_read(&pool, circle, &c).await,
        "new invitation is not access"
    );
    sqlx::query(
        "UPDATE domain_audience_circle_members
                 SET status='active', joined_at=NOW() WHERE circle_id=$1 AND account_id=$2",
    )
    .bind(circle)
    .bind(&c)
    .execute(&pool)
    .await
    .unwrap();
    assert!(
        can_read(&pool, circle, &c).await,
        "accepted membership is access"
    );
    sqlx::query("UPDATE domain_accounts SET disabled=TRUE WHERE id=$1")
        .bind(&c)
        .execute(&pool)
        .await
        .unwrap();
    assert!(
        !can_read(&pool, circle, &c).await,
        "disabled account must not read"
    );

    sqlx::query("DELETE FROM domain_audience_circles WHERE id=$1")
        .bind(circle)
        .execute(&pool)
        .await
        .unwrap();
    for actor in [&a, &b, &c] {
        sqlx::query("DELETE FROM domain_accounts WHERE id=$1")
            .bind(actor)
            .execute(&pool)
            .await
            .unwrap();
    }
}
