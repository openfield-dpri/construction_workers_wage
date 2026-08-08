start_time <- Sys.time()

system2(
  "python",
  c(
    "scraper.py",
    "--prefectures", "北海道",
    "--debug"
  )
)

end_time <- Sys.time()

end_time - start_time