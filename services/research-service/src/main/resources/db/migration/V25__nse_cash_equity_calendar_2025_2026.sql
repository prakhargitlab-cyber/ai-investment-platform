-- NSE Capital Market (cash equities), verified against official circulars.
-- Times are exchange-local Asia/Kolkata NORMAL MARKET times, excluding
-- pre-open, closing auctions, post-close and settlement-only holidays.
-- Keep applied V5 immutable. Existing operator/exception rows win on conflict;
-- the deployment verification must surface any disagreement with these sources.

-- NSE/CMTR/65587, 2024-12-13: annual 2025 trading holidays.
-- October 21 is represented below by its subsequent Muhurat session notice.
INSERT INTO market_trading_calendar_exceptions
    (market_code,trading_date,exception_type,open_time,close_time,reason,source_reference)
VALUES
 ('NSE','2025-01-26','CLOSED',NULL,NULL,'Republic Day','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-02-26','CLOSED',NULL,NULL,'Mahashivratri','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-03-14','CLOSED',NULL,NULL,'Holi','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-03-31','CLOSED',NULL,NULL,'Id-Ul-Fitr (Ramadan Eid)','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-04-06','CLOSED',NULL,NULL,'Shri Ram Navami','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-04-10','CLOSED',NULL,NULL,'Shri Mahavir Jayanti','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-04-14','CLOSED',NULL,NULL,'Dr. Baba Saheb Ambedkar Jayanti','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-04-18','CLOSED',NULL,NULL,'Good Friday','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-05-01','CLOSED',NULL,NULL,'Maharashtra Day','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-06-07','CLOSED',NULL,NULL,'Bakri Id','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-07-06','CLOSED',NULL,NULL,'Muharram','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-08-15','CLOSED',NULL,NULL,'Independence Day','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-08-27','CLOSED',NULL,NULL,'Ganesh Chaturthi','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-10-02','CLOSED',NULL,NULL,'Mahatma Gandhi Jayanti / Dussehra','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-10-22','CLOSED',NULL,NULL,'Diwali-Balipratipada','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-11-05','CLOSED',NULL,NULL,'Prakash Gurpurb Sri Guru Nanak Dev','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf'),
 ('NSE','2025-12-25','CLOSED',NULL,NULL,'Christmas','https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf')
ON CONFLICT DO NOTHING;

-- NSE/CMTR/71775, 2025-12-12: annual 2026 trading holidays.
-- November 8 is a Muhurat trading DATE, not an all-day market closure.
INSERT INTO market_trading_calendar_exceptions
    (market_code,trading_date,exception_type,open_time,close_time,reason,source_reference)
VALUES
 ('NSE','2026-01-26','CLOSED',NULL,NULL,'Republic Day','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-02-15','CLOSED',NULL,NULL,'Mahashivratri','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-03-03','CLOSED',NULL,NULL,'Holi','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-03-21','CLOSED',NULL,NULL,'Id-Ul-Fitr (Ramadan Eid)','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-03-26','CLOSED',NULL,NULL,'Shri Ram Navami','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-03-31','CLOSED',NULL,NULL,'Shri Mahavir Jayanti','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-04-03','CLOSED',NULL,NULL,'Good Friday','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-04-14','CLOSED',NULL,NULL,'Dr. Baba Saheb Ambedkar Jayanti','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-05-01','CLOSED',NULL,NULL,'Maharashtra Day','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-05-28','CLOSED',NULL,NULL,'Bakri Id','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-06-26','CLOSED',NULL,NULL,'Muharram','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-08-15','CLOSED',NULL,NULL,'Independence Day','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-09-14','CLOSED',NULL,NULL,'Ganesh Chaturthi','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-10-02','CLOSED',NULL,NULL,'Mahatma Gandhi Jayanti','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-10-20','CLOSED',NULL,NULL,'Dussehra','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-11-10','CLOSED',NULL,NULL,'Diwali-Balipratipada','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-11-24','CLOSED',NULL,NULL,'Prakash Gurpurb Sri Guru Nanak Dev','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf'),
 ('NSE','2026-12-25','CLOSED',NULL,NULL,'Christmas','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf')
ON CONFLICT DO NOTHING;

-- NSE/CMTR/72260, 2026-01-12: amendment to the annual 2026 calendar.
INSERT INTO market_trading_calendar_exceptions
    (market_code,trading_date,exception_type,open_time,close_time,reason,source_reference)
VALUES
 ('NSE','2026-01-15','CLOSED',NULL,NULL,'Municipal Corporation Election in Maharashtra','https://nsearchives.nseindia.com/content/circulars/CMTR72260.pdf')
ON CONFLICT DO NOTHING;

-- NSE/CMTR/65729 (2024-12-23), /70319 (2025-09-22), /72349 (2026-01-16).
-- Live trading, including the weekend Budget sessions, must require candles.
INSERT INTO market_trading_calendar_exceptions
    (market_code,trading_date,exception_type,open_time,close_time,reason,source_reference)
VALUES
 ('NSE','2025-02-01','SPECIAL_SESSION','09:15:00','15:30:00','Union Budget live trading','https://nsearchives.nseindia.com/content/circulars/CMTR65729.pdf'),
 ('NSE','2025-10-21','SPECIAL_SESSION','13:45:00','14:45:00','Diwali Muhurat live trading','https://nsearchives.nseindia.com/content/circulars/CMTR70319.pdf'),
 ('NSE','2026-02-01','SPECIAL_SESSION','09:15:00','15:30:00','Union Budget live trading','https://nsearchives.nseindia.com/content/circulars/CMTR72349.pdf'),
 -- /71775 confirms the future date but explicitly defers timings. NULL is
 -- intentional: session status is UNKNOWN; historical validation must also
 -- retain this date as unresolved once its local trading date is reached.
 -- Reconcile the official timing notice in a later version BEFORE 2026-11-08;
 -- do not infer 2026 hours from the 2025 session or declare this date CLOSED.
 ('NSE','2026-11-08','SPECIAL_SESSION',NULL,NULL,'Diwali Muhurat; official timings pending verification','https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf')
ON CONFLICT DO NOTHING;
