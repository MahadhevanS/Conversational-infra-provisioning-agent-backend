# Run this once as a standalone script to test
import smtplib

with smtplib.SMTP("smtp.gmail.com", 587) as server:
    server.starttls()
    server.login("sportspose300@gmail.com", "yrbl fcab zzmb okba")
    print("✅ SMTP login successful!")