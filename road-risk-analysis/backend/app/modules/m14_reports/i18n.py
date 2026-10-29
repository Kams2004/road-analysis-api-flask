"""Labels of the PDF reports (English, French, Spanish). Missing keys fall back to English."""

EN = {
    "app": "VIASafe Road Risk Analysis Platform",
    "generated": "Generated", "by": "by", "page": "Page", "report_id": "Report",
    "filters": "Filters", "summary": "Summary", "methodology": "Methodology & version",
    "period": "Period", "all_time": "All time", "types": "Types", "all_types": "All types",
    "zone": "Area", "town": "Town", "path": "Path", "corridor": "Corridor", "none": "None",
    "path_length": "Path length", "statuses": "Statuses",
    "map": "Map", "map_path_note": "Schematic map: the path with its km markers and the items (numbers = table rows).",
    "map_note": "Schematic map of the items (no background map).",
    "km": "km", "km_along": "Km", "offset": "Offset (m)",
    "no_data": "No data for these filters.",
    "image": "Image", "open_image": "Open full image", "open_console": "Open in console",
    "image_note": "Previews show the whole frame scaled down (never cropped); the link opens the full image in the "
                  "web console (sign-in required).",
    # hazards
    "hazards_title": "AI hazards report", "hazards": "Hazards", "code": "Code", "type": "Type",
    "location": "Location", "status": "Status", "detections": "Detections", "last_seen": "Last seen",
    "first_seen": "First seen", "confidence": "Confidence", "by_type": "By type", "by_status": "By status",
    "by_town": "By town", "recurring": "Recurring spots", "active": "Active", "resolved": "Resolved",
    "total_detections": "Detections (sightings)", "avg_conf": "Average confidence",
    "hazard_list": "Hazards list",
    # community
    "community_title": "Community reports report", "reports": "Reports", "reported_at": "Reported",
    "votes": "Votes (present / absent)", "uncertain": "Uncertain", "report_list": "Community reports list",
    "actif": "Active", "annule": "Cancelled", "rejete": "Rejected", "description": "Description",
    # historical
    "historical_title": "Historical risk report", "events": "Events", "cells": "Scored cells",
    "high_risk_cells": "High-risk cells (score >= 0.5)", "low_coverage": "Low-coverage cells (< 3 events)",
    "max_score": "Highest score", "accidents": "Accidents", "top_zones": "Top risk zones", "score": "Score",
    "coverage": "Coverage", "road": "Road", "trend": "Risk trend", "risk_points": "Risk points",
    "events_by_type": "Events by type", "route_risk": "Risk along the path",
    # exception patterns (anonymous)
    "patterns_title": "Exception patterns report", "patterns_kpis": "Exceptions by type",
    "vehicles": "Vehicles", "high_sev": "High severity", "avg_speed": "Avg speed (km/h)",
    "avg_limit": "Avg limit (km/h)", "avg_excess": "Avg excess (%)", "peak_hour": "Peak hour",
    "level": "Level", "patterns_zones": "Places where exceptions concentrate",
    "dominant": "Dominant type", "by_hour": "Exceptions by hour of day (local time)",
    "by_weekday": "Exceptions by day of week (local time)", "by_source": "Exceptions by source / provider",
    "by_country": "Exceptions by country", "by_severity": "Exceptions by severity",
    "by_speed_band": "Exceptions by recorded speed (km/h)", "by_excess_band": "Speeding by excess over the limit (%)",
    "places": "Places (~110 m)", "sources": "Sources", "unknown": "Unknown", "exceptions": "Exceptions",
    "wd0": "Mon", "wd1": "Tue", "wd2": "Wed", "wd3": "Thu", "wd4": "Fri", "wd5": "Sat", "wd6": "Sun",
    "count": "Count", "share": "Share",
    # collision
    "collision_title": "Collision prevention report", "registered": "Registered vehicles",
    "active_period": "Vehicles active in the period", "active_alerts": "Active alerts (now)",
    "alerts_by_level": "Active alerts by level", "vehicles_by_type": "Vehicles by type",
    "collision_note": "Collision alerts are live and short-lived (kept in memory for minutes); this report shows the "
                      "connected fleet and the alerts active when it was generated.",
    "SPEEDING": "Speeding", "HARD_BRAKING": "Harsh braking", "HARD_ACCELERATION": "Hard acceleration",
    "HARSH_CORNERING": "Harsh cornering", "ACCIDENT": "Accident", "IDLE_VIOLATION": "Idle violation",
    "OTHER": "Other",
    "low": "Low", "medium": "Medium", "high": "High", "good": "Good",
}

FR = {
    "generated": "Généré le", "by": "par", "page": "Page", "report_id": "Rapport",
    "filters": "Filtres", "summary": "Synthèse", "methodology": "Méthodologie et version",
    "period": "Période", "all_time": "Toute la période", "types": "Types", "all_types": "Tous les types",
    "zone": "Zone", "town": "Ville", "path": "Itinéraire", "corridor": "Corridor", "none": "Aucun",
    "path_length": "Longueur de l'itinéraire", "statuses": "Statuts",
    "map": "Carte", "map_path_note": "Carte schématique : l'itinéraire, ses repères kilométriques et les éléments "
                                     "(numéros = lignes du tableau).",
    "map_note": "Carte schématique des éléments (sans fond de carte).",
    "km_along": "Km", "offset": "Écart (m)", "no_data": "Aucune donnée pour ces filtres.",
    "image": "Image", "open_image": "Ouvrir l'image complète", "open_console": "Ouvrir dans la console",
    "image_note": "Les aperçus montrent l'image entière réduite (jamais rognée) ; le lien ouvre l'image complète "
                  "dans la console web (connexion requise).",
    "hazards_title": "Rapport des dangers détectés par l'IA", "hazards": "Dangers", "code": "Code", "type": "Type",
    "location": "Localisation", "status": "Statut", "detections": "Détections", "last_seen": "Vu en dernier",
    "first_seen": "Vu en premier", "confidence": "Confiance", "by_type": "Par type", "by_status": "Par statut",
    "by_town": "Par ville", "recurring": "Points récurrents", "active": "Actif", "resolved": "Résolu",
    "total_detections": "Détections (observations)", "avg_conf": "Confiance moyenne",
    "hazard_list": "Liste des dangers",
    "community_title": "Rapport des signalements communautaires", "reports": "Signalements",
    "reported_at": "Signalé le", "votes": "Votes (présent / absent)", "uncertain": "Incertain",
    "report_list": "Liste des signalements", "actif": "Actif", "annule": "Annulé", "rejete": "Rejeté",
    "description": "Description",
    "historical_title": "Rapport de risque historique", "events": "Événements", "cells": "Cellules notées",
    "high_risk_cells": "Cellules à risque élevé (score >= 0,5)", "low_coverage": "Cellules peu couvertes (< 3 événements)",
    "max_score": "Score maximal", "accidents": "Accidents", "top_zones": "Zones les plus à risque", "score": "Score",
    "coverage": "Couverture", "road": "Route", "trend": "Évolution du risque", "risk_points": "Points de risque",
    "events_by_type": "Événements par type", "route_risk": "Risque le long de l'itinéraire",
    "patterns_title": "Rapport des tendances d'exceptions", "patterns_kpis": "Exceptions par type",
    "vehicles": "Véhicules", "high_sev": "Gravité élevée",
    "avg_speed": "Vitesse moy. (km/h)", "avg_limit": "Limite moy. (km/h)", "avg_excess": "Dépassement moy. (%)",
    "peak_hour": "Heure de pointe", "level": "Niveau",
    "patterns_zones": "Lieux de concentration des exceptions", "dominant": "Type dominant",
    "by_hour": "Exceptions par heure (heure locale)", "by_weekday": "Exceptions par jour de la semaine (heure locale)",
    "by_source": "Exceptions par source / fournisseur", "by_country": "Exceptions par pays",
    "by_severity": "Exceptions par gravité", "by_speed_band": "Exceptions par vitesse relevée (km/h)",
    "by_excess_band": "Excès de vitesse par dépassement de la limite (%)", "places": "Lieux (~110 m)",
    "sources": "Sources", "unknown": "Inconnu", "exceptions": "Exceptions",
    "wd0": "Lun", "wd1": "Mar", "wd2": "Mer", "wd3": "Jeu", "wd4": "Ven", "wd5": "Sam", "wd6": "Dim",
    "count": "Nombre", "share": "Part",
    "collision_title": "Rapport de prévention des collisions", "registered": "Véhicules enregistrés",
    "active_period": "Véhicules actifs sur la période", "active_alerts": "Alertes actives (maintenant)",
    "alerts_by_level": "Alertes actives par niveau", "vehicles_by_type": "Véhicules par type",
    "collision_note": "Les alertes de collision sont en temps réel et de courte durée (quelques minutes en mémoire) ; "
                      "ce rapport présente la flotte connectée et les alertes actives au moment de sa génération.",
    "SPEEDING": "Excès de vitesse", "HARD_BRAKING": "Freinage brusque", "HARD_ACCELERATION": "Accélération brusque",
    "HARSH_CORNERING": "Virage brusque", "ACCIDENT": "Accident", "IDLE_VIOLATION": "Ralenti excessif",
    "OTHER": "Autre", "low": "Faible", "medium": "Moyen", "high": "Élevé", "good": "Bonne",
}

ES = {
    "generated": "Generado el", "by": "por", "page": "Página", "report_id": "Informe",
    "filters": "Filtros", "summary": "Resumen", "methodology": "Metodología y versión",
    "period": "Periodo", "all_time": "Todo el periodo", "types": "Tipos", "all_types": "Todos los tipos",
    "zone": "Zona", "town": "Ciudad", "path": "Ruta", "corridor": "Corredor", "none": "Ninguno",
    "path_length": "Longitud de la ruta", "statuses": "Estados",
    "map": "Mapa", "map_path_note": "Mapa esquemático: la ruta con sus hitos kilométricos y los elementos "
                                    "(números = filas de la tabla).",
    "map_note": "Mapa esquemático de los elementos (sin mapa de fondo).",
    "offset": "Desvío (m)", "no_data": "No hay datos para estos filtros.",
    "image": "Imagen", "open_image": "Abrir la imagen completa", "open_console": "Abrir en la consola",
    "image_note": "Las vistas previas muestran la imagen entera reducida (nunca recortada); el enlace abre la imagen "
                  "completa en la consola web (requiere iniciar sesión).",
    "hazards_title": "Informe de peligros detectados por IA", "hazards": "Peligros", "code": "Código", "type": "Tipo",
    "location": "Ubicación", "status": "Estado", "detections": "Detecciones", "last_seen": "Visto por última vez",
    "first_seen": "Visto por primera vez", "confidence": "Confianza", "by_type": "Por tipo", "by_status": "Por estado",
    "by_town": "Por ciudad", "recurring": "Puntos recurrentes", "active": "Activo", "resolved": "Resuelto",
    "total_detections": "Detecciones (observaciones)", "avg_conf": "Confianza media", "hazard_list": "Lista de peligros",
    "community_title": "Informe de reportes comunitarios", "reports": "Reportes", "reported_at": "Reportado",
    "votes": "Votos (presente / ausente)", "uncertain": "Incierto", "report_list": "Lista de reportes",
    "actif": "Activo", "annule": "Cancelado", "rejete": "Rechazado", "description": "Descripción",
    "historical_title": "Informe de riesgo histórico", "events": "Eventos", "cells": "Celdas puntuadas",
    "high_risk_cells": "Celdas de alto riesgo (puntuación >= 0,5)", "low_coverage": "Celdas con poca cobertura (< 3 eventos)",
    "max_score": "Puntuación máxima", "accidents": "Accidentes", "top_zones": "Zonas de mayor riesgo",
    "score": "Puntuación", "coverage": "Cobertura", "road": "Vía", "trend": "Evolución del riesgo",
    "risk_points": "Puntos de riesgo", "events_by_type": "Eventos por tipo", "route_risk": "Riesgo a lo largo de la ruta",
    "patterns_title": "Informe de patrones de excepciones", "patterns_kpis": "Excepciones por tipo",
    "vehicles": "Vehículos", "high_sev": "Gravedad alta", "avg_speed": "Velocidad media (km/h)",
    "avg_limit": "Límite medio (km/h)", "avg_excess": "Exceso medio (%)", "peak_hour": "Hora pico",
    "level": "Nivel", "patterns_zones": "Lugares donde se concentran las excepciones",
    "dominant": "Tipo dominante", "by_hour": "Excepciones por hora del día (hora local)",
    "by_weekday": "Excepciones por día de la semana (hora local)",
    "by_source": "Excepciones por fuente / proveedor", "by_country": "Excepciones por país",
    "by_severity": "Excepciones por gravedad", "by_speed_band": "Excepciones por velocidad registrada (km/h)",
    "by_excess_band": "Excesos de velocidad por exceso sobre el límite (%)", "places": "Lugares (~110 m)",
    "sources": "Fuentes", "unknown": "Desconocido", "exceptions": "Excepciones",
    "wd0": "Lun", "wd1": "Mar", "wd2": "Mié", "wd3": "Jue", "wd4": "Vie", "wd5": "Sáb", "wd6": "Dom",
    "count": "Número", "share": "Proporción",
    "collision_title": "Informe de prevención de colisiones", "registered": "Vehículos registrados",
    "active_period": "Vehículos activos en el periodo", "active_alerts": "Alertas activas (ahora)",
    "alerts_by_level": "Alertas activas por nivel", "vehicles_by_type": "Vehículos por tipo",
    "collision_note": "Las alertas de colisión son en tiempo real y de corta duración (minutos en memoria); este informe "
                      "muestra la flota conectada y las alertas activas al generarlo.",
    "SPEEDING": "Exceso de velocidad", "HARD_BRAKING": "Frenada brusca", "HARD_ACCELERATION": "Aceleración brusca",
    "HARSH_CORNERING": "Giro brusco", "ACCIDENT": "Accidente", "IDLE_VIOLATION": "Ralentí excesivo", "OTHER": "Otro",
    "low": "Bajo", "medium": "Medio", "high": "Alto", "good": "Buena",
}

HAZARD_TYPES = {
    "en": {"pothole": "Pothole", "speed_bump": "Speed bump", "traffic_sign": "Traffic sign",
           "parked_vehicle": "Parked vehicle"},
    "fr": {"pothole": "Nid-de-poule", "speed_bump": "Dos-d'âne", "traffic_sign": "Panneau de signalisation",
           "parked_vehicle": "Véhicule stationné"},
    "es": {"pothole": "Bache", "speed_bump": "Reductor de velocidad", "traffic_sign": "Señal de tráfico",
           "parked_vehicle": "Vehículo estacionado"},
}

_DICTS = {"en": EN, "fr": {**EN, **FR}, "es": {**EN, **ES}}


def translator(lang: str):
    d = _DICTS.get(lang, EN)
    types = {**HAZARD_TYPES["en"], **HAZARD_TYPES.get(lang, {})}

    def t(key: str) -> str:
        if key in d:
            return d[key]
        if key in types:
            return types[key]
        return key.replace("_", " ").capitalize() if key and key.islower() else key
    return t
