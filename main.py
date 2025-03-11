def apply_targeting_criteria(client: GoogleAdsClient, customer_id: str, campaign_resource_name: str, data: CampaignRequest):
    logging.info("Aplicando targeting na Campaign.")
    campaign_criterion_service = client.get_service("CampaignCriterionService")
    operations = []
    # Gênero: somente adicione se for 'MALE' ou 'FEMALE'
    if data.audience_gender and data.audience_gender.upper() in ["MALE", "FEMALE"]:
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = campaign_resource_name
        criterion.gender.type_ = client.enums.GenderTypeEnum[data.audience_gender.upper()]
        # Explicitamente forçando o critério a ser inclusivo
        criterion.negative = False
        criterion.status = client.enums.CampaignCriterionStatusEnum.ENABLED
        operations.append(op)
    # Faixa etária: somente se os limites fizerem sentido
    if data.audience_min_age <= 18 <= data.audience_max_age:
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = campaign_resource_name
        criterion.age_range.type_ = client.enums.AgeRangeTypeEnum.AGE_RANGE_18_24
        criterion.negative = False
        criterion.status = client.enums.CampaignCriterionStatusEnum.ENABLED
        operations.append(op)
    # Dispositivos: adicione apenas os dispositivos válidos
    valid_devices = {"SMARTPHONE", "DESKTOP", "TABLET"}
    for d in data.devices:
        if d and d.strip().upper() in valid_devices:
            op = client.get_type("CampaignCriterionOperation")
            criterion = op.create
            criterion.campaign = campaign_resource_name
            criterion.device.type_ = client.enums.DeviceEnum[d.strip().upper()]
            criterion.negative = False
            criterion.status = client.enums.CampaignCriterionStatusEnum.ENABLED
            operations.append(op)
    if operations:
        response = campaign_criterion_service.mutate_campaign_criteria(
            customer_id=customer_id, operations=operations
        )
        for result in response.results:
            logging.info(f"Campaign Criterion criado: {result.resource_name}")
