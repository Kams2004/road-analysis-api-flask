"""
Module 14 — Reports (RRAP-13-011/012/013): PDF reports generated server-side.

  catalogue.py   report types, their data module (permission) and filter options
  pathfilter.py  path filter: road section / from → to / waypoints → polyline,
                 corridor membership and distance along the path
  collectors.py  data of each report type (hazards, community, historical,
                 exception patterns — anonymous —, collision prevention)
  pdf.py         reportlab rendering (tables, charts, schematic path map,
                 aspect-preserving image previews + links to the console)
  models/        generated_reports (metadata; files in the MinIO bucket `reports`)
  routes/        /reports
"""
