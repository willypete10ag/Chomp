-- Open Food Facts name search (#88) caches every hit it shows, so an OFF row no longer means
-- someone chose it: like FDC hits, OFF foods now count as chosen only once saved, confirmed,
-- logged, portioned or in a template. Every OFF row so far came from a barcode lookup, which
-- is a deliberate choice, so mark them saved to keep them in Foods.
UPDATE foods SET saved = 1 WHERE source = 'off';
