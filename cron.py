"""Daily scan report cron job for QRaft."""

import os
import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/qraft")


def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT tagline, url, total_scans FROM campaigns ORDER BY total_scans DESC")
    rows = cur.fetchall()

    if not rows:
        print("No campaigns found.")
        cur.close()
        conn.close()
        return

    print("=" * 70)
    print("QRaft Daily Scan Report")
    print("=" * 70)
    print(f"{'Tagline':<30} {'URL':<25} {'Scans':>8}")
    print("-" * 70)

    for row in rows:
        tagline = (row["tagline"][:27] + "...") if len(row["tagline"]) > 30 else row["tagline"]
        url = (row["url"][:22] + "...") if len(row["url"]) > 25 else row["url"]
        print(f"{tagline:<30} {url:<25} {row['total_scans']:>8}")

    print("-" * 70)
    top = rows[0]
    print(f"\nTop campaign: \"{top['tagline']}\" with {top['total_scans']} scans")
    print(f"  -> {top['url']}")
    print()

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
