def process_logo_image(logo_path: str) -> bytes:
    """
    Carrega o logotipo a partir do arquivo padrao.jpg, remove metadados e força a imagem
    a ter exatamente 1200x1200 pixels. A imagem é salva como JPEG para garantir compatibilidade.
    Se ocorrer erro, gera uma imagem branca de 1200x1200.
    """
    try:
        if os.path.exists(logo_path):
            with Image.open(logo_path) as img:
                # Corrige a orientação baseada em EXIF e converte para RGB
                img = ImageOps.exif_transpose(img).convert("RGB")
                logging.debug(f"Logo original: {img.size}")
                # Força o recorte e redimensionamento para 1200x1200 usando ImageOps.fit
                img = ImageOps.fit(img, (1200, 1200), method=Image.LANCZOS)
        else:
            logging.warning(f"Arquivo {logo_path} não encontrado. Gerando logotipo em branco.")
            img = Image.new("RGB", (1200, 1200), (255, 255, 255))
    except Exception as e:
        logging.error(f"Erro ao abrir {logo_path}: {e}. Gerando logotipo em branco.")
        img = Image.new("RGB", (1200, 1200), (255, 255, 255))
    
    buf = BytesIO()
    # Salva como JPEG (sem metadados) para garantir o aspecto correto
    img.save(buf, format="JPEG", quality=95)
    processed_data = buf.getvalue()
    logging.debug(f"Logo processada: tamanho {img.size}, {len(processed_data)} bytes")
    return processed_data
