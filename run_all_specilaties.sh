# Siehe Beispiel  
# python3 gemini_clarity_digest.py --specialty-dir cardiology_digest --live &    # PRODUKTIV (überschreibt Config)
# python3 gemini_clarity_digest.py --specialty-dir cardiology_digest &           # TEST (Config-Default true greift)
python3 gemini_clarity_digest.py --specialty-dir ./specialties/cardiology_digest --live &      # PRODUKTIV (überschreibt Config)
# python3 gemini_clarity_digest.py --specialty-dir dermatology_digest &                        # TEST (Config-Default true greift)
# python3 gemini_clarity_digest.py --specialty-dir pulmonology_digest --live &                 # PRODUKTIV
# python3 gemini_clarity_digest.py --specialty-dir internal_digest &                           # TEST