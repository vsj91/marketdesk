"""Broad sector groups for caps and exposure. Provider sector names vary ("Bank", "Banks", "Finance"), so limits use these groups."""

GROUPS={
 'Financials':('bank','banks','finance','financial services','nbfc','housing finance','insurance','asset management','stock broking',
  'stock/ commodity brokers','ratings','investment'),
 'Energy':('oil exploration','crude oil & natural gas','refineries','gas transmission','gases & fuels','lubricants','oil drill/allied'),
 'Power':('power','power generation & distribution','power infrastructure','transmission towers'),
 'Metals & Mining':('metal','metals','steel','steel & iron products','non ferrous metals','aluminium products','minerals','mining & mineral products'),
 'Information Technology':('it - software','it-software','bpo/ites','it - hardware','it - networking','it - education'),
 'Healthcare':('pharmaceuticals','healthcare','healthcare services','medical equipment'),
 'Automobiles':('automobile','automobiles','auto ancillaries','auto ancillary','tyres','tyres & allied','cycles','batteries'),
 'Industrials':('capital goods','capital goods - electrical equipment','capital goods-non electrical equipment','electric equipment','engineering',
  'compressors','diesel engines','bearings','castings','castings, forgings & fasteners','forgings','fasteners','welding equipment','abrasives',
  'cable','cables','electronics','defence','aerospace & defence','ship building','railways wagons'),
 'Construction & Materials':('construction','construction materials','cement','infrastructure','infrastructure developers & operators','ceramics',
  'ceramic products','glass','plywood boards/laminates','wood products','refractories','packaging','paper','plastic products'),
 'Real Estate':('realty','real estate investment trusts','co-working'),
 'Chemicals':('chemicals','agro chemicals','agrochemicals','fertilizers','dyes & pigments','carbon black','petrochemicals'),
 'Consumer Staples':('fmcg','consumer food','household products','tea/coffee','tobacco','tobacco products','edible oil','sugar','breweries',
  'alcoholic beverages','solvent extraction','agriculture','aquaculture'),
 'Consumer Discretionary':('consumer durables','air conditioners','retailing','e-commerce','e-commerce/app based aggregator','jewellery',
  'diamond, gems and jewellery','watches & accessories','footwear','textile','textiles','leather','hotel','hotels & restaurants',
  'quick service restaurant','travel services','recreation','education','educational institutions','paints','decoratives','stationery',
  'photographic products'),
 'Media & Telecom':('telecommunication','telecom-service','telecom equipment & infra services','tv broadcasting','film production','entertainment',
  'printing and publishing'),
 'Transport & Logistics':('logistics','courier services','port','shipping','airlines'),
}
# Listed REITs whose provider label or truncated name hides that they are REITs.
REITS={'BIRET','EMBASSY','MINDSPACE','NXST','BAGMANE'}
LOOKUP={raw:group for group,names in GROUPS.items() for raw in names}


def sector_group(raw,name=None,symbol=None):
    """Broad group for a provider sector; REITs are Real Estate whatever their provider label. Unknown names pass through."""
    if symbol in REITS:return 'Real Estate'
    if name and ('REIT' in name.upper().split() or 'REAL ESTATE INVESTMENT TRUST' in name.upper() or 'REAL ESTATE TRUST' in name.upper()):return 'Real Estate'
    raw=(raw or '').strip()
    return LOOKUP.get(raw.casefold(),raw) or 'Unknown'
