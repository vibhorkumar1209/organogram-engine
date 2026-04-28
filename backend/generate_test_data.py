"""Generate test_data.json — 100 mixed-sector records spanning 10 layers."""
import json, random
from pathlib import Path

random.seed(42)

RECORDS = [
  # ── AUTOMOTIVE (20) ─────────────────────────────────
  {"FirstName":"Elena","LastName":"Voss","Designation":"Chairman & CEO","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/elenavoss","Location":"Detroit, USA","Industry_Hint":"automotive"},
  {"FirstName":"Marcus","LastName":"Holt","Designation":"SVP Global Supply Chain","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/marcusholt","Location":"Detroit, USA","Industry_Hint":"automotive"},
  {"FirstName":"Priya","LastName":"Ramesh","Designation":"Vice President, EV Engineering","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/priyaramesh","Location":"Detroit, USA","Industry_Hint":"automotive ev battery"},
  {"FirstName":"James","LastName":"Park","Designation":"Plant Manager – Michigan Assembly","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/jamespark","Location":"Detroit, USA","Industry_Hint":"automotive manufacturing"},
  {"FirstName":"Sonia","LastName":"Wren","Designation":"Senior Director, PowerTrain Systems","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/soniawren","Location":"Detroit, USA","Industry_Hint":"automotive powertrain"},
  {"FirstName":"Kai","LastName":"Tanaka","Designation":"Director, EV Battery Systems","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/kaitanaka","Location":"Tokyo, Japan","Industry_Hint":"automotive ev battery"},
  {"FirstName":"Nina","LastName":"Schulz","Designation":"Senior Manager, Autonomous Systems","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/ninaschulz","Location":"Berlin, Germany","Industry_Hint":"automotive autonomous"},
  {"FirstName":"Leo","LastName":"Ferreira","Designation":"Manager, ADAS Validation","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/leoferreira","Location":"Berlin, Germany","Industry_Hint":"automotive autonomous adas"},
  {"FirstName":"Aisha","LastName":"Nkosi","Designation":"Senior Engineer, Battery Cell Development","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/aishanKosi","Location":"Johannesburg, South Africa","Industry_Hint":"automotive ev battery"},
  {"FirstName":"Omar","LastName":"Al-Rashid","Designation":"Engineer, Embedded Firmware","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/omaralrashid","Location":"Dubai, UAE","Industry_Hint":"automotive firmware embedded"},
  {"FirstName":"Hana","LastName":"Müller","Designation":"Junior Engineer, Transmission","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/hanamuller","Location":"Munich, Germany","Industry_Hint":"automotive powertrain drivetrain"},
  {"FirstName":"Raj","LastName":"Patel","Designation":"Intern, EV Battery Research","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/rajpatel","Location":"Pune, India","Industry_Hint":"automotive ev battery"},
  {"FirstName":"Chloe","LastName":"Dubois","Designation":"VP, European Operations","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/chlodubois","Location":"Paris, France","Industry_Hint":"automotive"},
  {"FirstName":"Stefan","LastName":"Bauer","Designation":"Director, Manufacturing QA","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/stefanbauer","Location":"Stuttgart, Germany","Industry_Hint":"automotive quality assurance"},
  {"FirstName":"Amara","LastName":"Diallo","Designation":"Senior Analyst, Supply Chain Analytics","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/amaradiallo","Location":"Lagos, Nigeria","Industry_Hint":"automotive supply chain"},
  {"FirstName":"Tom","LastName":"Briggs","Designation":"COO","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/tombriggs","Location":"Detroit, USA","Industry_Hint":"automotive"},
  {"FirstName":"Yuki","LastName":"Hara","Designation":"Senior Director, Asia Pacific Sales","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/yukihara","Location":"Tokyo, Japan","Industry_Hint":"automotive sales"},
  {"FirstName":"Carlos","LastName":"Mendez","Designation":"Manager, Procurement","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/carlosmendez","Location":"Mexico City, Mexico","Industry_Hint":"automotive supply chain procurement"},
  {"FirstName":"Linda","LastName":"Osei","Designation":"Trainee, Production Assembly","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/lindaosei","Location":"Accra, Ghana","Industry_Hint":"automotive manufacturing"},
  {"FirstName":"Dev","LastName":"Sharma","Designation":"Principal Engineer, Sensor Fusion","Company":"AutoPrime Motors","LinkedInURL":"https://linkedin.com/in/devsharma","Location":"Bangalore, India","Industry_Hint":"automotive autonomous sensor fusion"},

  # ── GOVERNMENT (20) ─────────────────────────────────
  {"FirstName":"Robert","LastName":"Clarke","Designation":"Secretary General, Ministry of Finance","Company":"Government of UK","LinkedInURL":"https://linkedin.com/in/robertclarke","Location":"London, UK","Industry_Hint":"government ministry"},
  {"FirstName":"Fatima","LastName":"Al-Hussein","Designation":"Under Secretary, Trade Affairs","Company":"Ministry of Trade","LinkedInURL":"https://linkedin.com/in/fatimahussein","Location":"Riyadh, Saudi Arabia","Industry_Hint":"government ministry"},
  {"FirstName":"Arjun","LastName":"Menon","Designation":"Bureau Chief, Revenue Intelligence","Company":"Government of India","LinkedInURL":"https://linkedin.com/in/arjunmenon","Location":"Delhi, India","Industry_Hint":"government bureau"},
  {"FirstName":"Sarah","LastName":"Fontaine","Designation":"Director General, Public Health","Company":"French Ministry of Health","LinkedInURL":"https://linkedin.com/in/sarahfontaine","Location":"Paris, France","Industry_Hint":"government policy"},
  {"FirstName":"Ahmed","LastName":"Khalid","Designation":"Additional Secretary, Infrastructure","Company":"Government of Pakistan","LinkedInURL":"https://linkedin.com/in/ahmedkhalid","Location":"Islamabad, Pakistan","Industry_Hint":"government"},
  {"FirstName":"Grace","LastName":"Otieno","Designation":"Joint Secretary, Education Department","Company":"Kenya Government","LinkedInURL":"https://linkedin.com/in/graceotieno","Location":"Nairobi, Kenya","Industry_Hint":"government"},
  {"FirstName":"Ivan","LastName":"Petrov","Designation":"Deputy Director, Customs Agency","Company":"Russian Federation","LinkedInURL":"https://linkedin.com/in/ivanpetrov","Location":"Moscow, Russia","Industry_Hint":"government"},
  {"FirstName":"Mei","LastName":"Zhang","Designation":"Senior Policy Analyst","Company":"China Ministry of Commerce","LinkedInURL":"https://linkedin.com/in/meizhang","Location":"Beijing, China","Industry_Hint":"government policy"},
  {"FirstName":"Bimpe","LastName":"Adeyemi","Designation":"Policy Officer, Economic Affairs","Company":"Nigerian Government","LinkedInURL":"https://linkedin.com/in/bimpeadeyemi","Location":"Abuja, Nigeria","Industry_Hint":"government"},
  {"FirstName":"Lars","LastName":"Eriksson","Designation":"Junior Policy Advisor","Company":"Swedish Government","LinkedInURL":"https://linkedin.com/in/larseriksson","Location":"Stockholm, Sweden","Industry_Hint":"government policy"},
  {"FirstName":"Neha","LastName":"Gupta","Designation":"District Collector","Company":"State Government Maharashtra","LinkedInURL":"https://linkedin.com/in/nehagupta","Location":"Mumbai, India","Industry_Hint":"government"},
  {"FirstName":"Thomas","LastName":"Fischer","Designation":"Federal Bureau Chief, Statistics","Company":"German Federal Government","LinkedInURL":"https://linkedin.com/in/thomasfischer","Location":"Berlin, Germany","Industry_Hint":"government bureau"},
  {"FirstName":"Ana","LastName":"Vasquez","Designation":"Under Secretary, Environment Ministry","Company":"Government of Colombia","LinkedInURL":"https://linkedin.com/in/anavasquez","Location":"Bogotá, Colombia","Industry_Hint":"government"},
  {"FirstName":"Kofi","LastName":"Asante","Designation":"President & Head of State","Company":"Republic of Ghana","LinkedInURL":"https://linkedin.com/in/kofiasante","Location":"Accra, Ghana","Industry_Hint":"government"},
  {"FirstName":"Nora","LastName":"Walsh","Designation":"Head of Regulatory Affairs","Company":"Irish Government","LinkedInURL":"https://linkedin.com/in/norawalsh","Location":"Dublin, Ireland","Industry_Hint":"government regulatory"},
  {"FirstName":"Ren","LastName":"Yoshida","Designation":"Vice Minister, Digital Transformation","Company":"Japan Ministry of Digital Affairs","LinkedInURL":"https://linkedin.com/in/renyoshida","Location":"Tokyo, Japan","Industry_Hint":"government"},
  {"FirstName":"David","LastName":"Okonkwo","Designation":"Trainee Policy Researcher","Company":"Nigerian Government","LinkedInURL":"https://linkedin.com/in/davidokonkwo","Location":"Lagos, Nigeria","Industry_Hint":"government policy"},
  {"FirstName":"Amelia","LastName":"Grant","Designation":"Senior Legal Counsel","Company":"US Department of Justice","LinkedInURL":"https://linkedin.com/in/ameliagrant","Location":"Washington DC, USA","Industry_Hint":"government legal"},
  {"FirstName":"Hassan","LastName":"Ibrahim","Designation":"Director, Urban Planning","Company":"Dubai Municipality","LinkedInURL":"https://linkedin.com/in/hassanibrahim","Location":"Dubai, UAE","Industry_Hint":"government"},
  {"FirstName":"Pita","LastName":"Ravuiwasa","Designation":"Cabinet Secretary","Company":"Fiji Government","LinkedInURL":"https://linkedin.com/in/pitaravuiwasa","Location":"Suva, Fiji","Industry_Hint":"government"},

  # ── NGO (15) ─────────────────────────────────────────
  {"FirstName":"Claire","LastName":"Beaumont","Designation":"Executive Director","Company":"Global Relief Foundation","LinkedInURL":"https://linkedin.com/in/clairebeaumont","Location":"Geneva, Switzerland","Industry_Hint":"ngo nonprofit"},
  {"FirstName":"Sipho","LastName":"Dlamini","Designation":"Country Director, South Africa","Company":"UNICEF","LinkedInURL":"https://linkedin.com/in/siphodlamini","Location":"Johannesburg, South Africa","Industry_Hint":"ngo unicef"},
  {"FirstName":"Lina","LastName":"Torres","Designation":"Senior Program Manager, Health","Company":"WHO","LinkedInURL":"https://linkedin.com/in/linatorres","Location":"Geneva, Switzerland","Industry_Hint":"ngo who"},
  {"FirstName":"James","LastName":"Owusu","Designation":"Field Coordinator, West Africa","Company":"Oxfam","LinkedInURL":"https://linkedin.com/in/jamesowusu","Location":"Accra, Ghana","Industry_Hint":"ngo oxfam"},
  {"FirstName":"Preethi","LastName":"Nair","Designation":"Data Analyst, Impact Measurement","Company":"World Bank","LinkedInURL":"https://linkedin.com/in/preethinair","Location":"Washington DC, USA","Industry_Hint":"ngo world bank analytics"},
  {"FirstName":"Tobias","LastName":"Richter","Designation":"Head of Communications","Company":"Greenpeace International","LinkedInURL":"https://linkedin.com/in/tobiasrichter","Location":"Amsterdam, Netherlands","Industry_Hint":"ngo communications"},
  {"FirstName":"Ada","LastName":"Okafor","Designation":"Grant Writer","Company":"Africa Health Fund","LinkedInURL":"https://linkedin.com/in/adaokafor","Location":"Nairobi, Kenya","Industry_Hint":"ngo nonprofit"},
  {"FirstName":"Sun","LastName":"Li","Designation":"VP, Asia Programs","Company":"Save the Children","LinkedInURL":"https://linkedin.com/in/sunli","Location":"Singapore","Industry_Hint":"ngo nonprofit"},
  {"FirstName":"Maria","LastName":"Santos","Designation":"Intern, Research","Company":"UNICEF","LinkedInURL":"https://linkedin.com/in/mariasantos","Location":"São Paulo, Brazil","Industry_Hint":"ngo unicef"},
  {"FirstName":"Kwame","LastName":"Appiah","Designation":"Trustee & Board Chairman","Company":"Pan-African Development Trust","LinkedInURL":"https://linkedin.com/in/kwameappiah","Location":"Accra, Ghana","Industry_Hint":"ngo trust"},
  {"FirstName":"Ingrid","LastName":"Solberg","Designation":"Director, Climate Programs","Company":"WWF","LinkedInURL":"https://linkedin.com/in/ingridsolberg","Location":"Oslo, Norway","Industry_Hint":"ngo climate"},
  {"FirstName":"Joseph","LastName":"Kimani","Designation":"Community Outreach Officer","Company":"Red Cross Kenya","LinkedInURL":"https://linkedin.com/in/josephkimani","Location":"Nairobi, Kenya","Industry_Hint":"ngo"},
  {"FirstName":"Hira","LastName":"Baig","Designation":"Senior M&E Specialist","Company":"USAID Pakistan","LinkedInURL":"https://linkedin.com/in/hirabaig","Location":"Islamabad, Pakistan","Industry_Hint":"ngo analytics"},
  {"FirstName":"Remy","LastName":"Leclerc","Designation":"Associate, Partnerships","Company":"Alliance for Climate Action","LinkedInURL":"https://linkedin.com/in/remyleclerc","Location":"Paris, France","Industry_Hint":"ngo partnership"},
  {"FirstName":"Zara","LastName":"Ahmed","Designation":"Chief Operating Officer","Company":"Qatar Foundation","LinkedInURL":"https://linkedin.com/in/zaraahmed","Location":"Doha, Qatar","Industry_Hint":"ngo foundation"},

  # ── STARTUP (15) ──────────────────────────────────────
  {"FirstName":"Alex","LastName":"Vance","Designation":"Co-Founder & CEO","Company":"NeuralEdge AI","LinkedInURL":"https://linkedin.com/in/alexvance","Location":"San Francisco, USA","Industry_Hint":"startup ai"},
  {"FirstName":"Jordan","LastName":"Kim","Designation":"CTO","Company":"NeuralEdge AI","LinkedInURL":"https://linkedin.com/in/jordankim","Location":"San Francisco, USA","Industry_Hint":"startup ai engineering"},
  {"FirstName":"Tara","LastName":"Singh","Designation":"Head of Product","Company":"NeuralEdge AI","LinkedInURL":"https://linkedin.com/in/tarasingh","Location":"San Francisco, USA","Industry_Hint":"startup product management"},
  {"FirstName":"Felix","LastName":"Braun","Designation":"VP Engineering","Company":"CloudStack GmbH","LinkedInURL":"https://linkedin.com/in/felixbraun","Location":"Berlin, Germany","Industry_Hint":"startup cloud engineering"},
  {"FirstName":"Nadia","LastName":"Hassan","Designation":"Senior Machine Learning Engineer","Company":"NeuralEdge AI","LinkedInURL":"https://linkedin.com/in/nadiahassan","Location":"Toronto, Canada","Industry_Hint":"startup machine learning ai"},
  {"FirstName":"Luca","LastName":"Romano","Designation":"Growth Manager","Company":"FinFlow Startup","LinkedInURL":"https://linkedin.com/in/lucaromano","Location":"Milan, Italy","Industry_Hint":"startup growth"},
  {"FirstName":"Aiko","LastName":"Fujiwara","Designation":"Software Engineer – Backend","Company":"NeuralEdge AI","LinkedInURL":"https://linkedin.com/in/aikofujiwara","Location":"Tokyo, Japan","Industry_Hint":"startup backend software"},
  {"FirstName":"Ben","LastName":"Carter","Designation":"Senior Data Scientist","Company":"DataSpark Startup","LinkedInURL":"https://linkedin.com/in/bencarter","Location":"London, UK","Industry_Hint":"startup data science ai"},
  {"FirstName":"Zoe","LastName":"Blackwell","Designation":"UX Designer","Company":"NeuralEdge AI","LinkedInURL":"https://linkedin.com/in/zoeblackwell","Location":"Austin, USA","Industry_Hint":"startup ux design"},
  {"FirstName":"Raj","LastName":"Kapoor","Designation":"Intern, Product","Company":"NeuralEdge AI","LinkedInURL":"https://linkedin.com/in/rajkapoor","Location":"Bangalore, India","Industry_Hint":"startup product"},
  {"FirstName":"Serena","LastName":"Webb","Designation":"Director of Sales","Company":"SaaSify Inc","LinkedInURL":"https://linkedin.com/in/serenawebb","Location":"New York, USA","Industry_Hint":"startup sales"},
  {"FirstName":"Hugo","LastName":"Leclaire","Designation":"Senior Frontend Developer","Company":"CloudStack GmbH","LinkedInURL":"https://linkedin.com/in/hugoleclaire","Location":"Paris, France","Industry_Hint":"startup frontend developer"},
  {"FirstName":"Mia","LastName":"Thompson","Designation":"Chief Revenue Officer","Company":"SaaSify Inc","LinkedInURL":"https://linkedin.com/in/miathompson","Location":"New York, USA","Industry_Hint":"startup revenue sales"},
  {"FirstName":"Eli","LastName":"Stone","Designation":"DevOps Engineer","Company":"NeuralEdge AI","LinkedInURL":"https://linkedin.com/in/elisstone","Location":"Seattle, USA","Industry_Hint":"startup devops cloud"},
  {"FirstName":"Ifeoma","LastName":"Chukwu","Designation":"Junior AI Researcher","Company":"Lagos AI Startup","LinkedInURL":"https://linkedin.com/in/ifeomachukwu","Location":"Lagos, Nigeria","Industry_Hint":"startup ai research"},

  # ── PUBLIC COMPANY (15) ───────────────────────────────
  {"FirstName":"William","LastName":"Crawford","Designation":"Chairman, Board of Directors","Company":"Meridian Corp Inc.","LinkedInURL":"https://linkedin.com/in/williamcrawford","Location":"Chicago, USA","Industry_Hint":"public company"},
  {"FirstName":"Susan","LastName":"Hartley","Designation":"Chief Financial Officer","Company":"Meridian Corp Inc.","LinkedInURL":"https://linkedin.com/in/susanhartley","Location":"Chicago, USA","Industry_Hint":"public finance"},
  {"FirstName":"Antonio","LastName":"Ricci","Designation":"EVP, International Operations","Company":"Meridian Corp Inc.","LinkedInURL":"https://linkedin.com/in/antonioricci","Location":"Rome, Italy","Industry_Hint":"public operations"},
  {"FirstName":"Diane","LastName":"Foster","Designation":"SVP, Human Resources","Company":"Meridian Corp Inc.","LinkedInURL":"https://linkedin.com/in/dianefoster","Location":"Chicago, USA","Industry_Hint":"public hr"},
  {"FirstName":"Rajan","LastName":"Nair","Designation":"Vice President, Technology","Company":"Meridian Corp Inc.","LinkedInURL":"https://linkedin.com/in/rajannair","Location":"Bangalore, India","Industry_Hint":"public technology engineering"},
  {"FirstName":"Catherine","LastName":"Dumont","Designation":"Senior Director, Legal Affairs","Company":"Meridian Corp Inc.","LinkedInURL":"https://linkedin.com/in/catherinedumont","Location":"Paris, France","Industry_Hint":"public legal"},
  {"FirstName":"Michael","LastName":"Adebayo","Designation":"Director, Risk Management","Company":"Meridian Corp Inc.","LinkedInURL":"https://linkedin.com/in/michaeladebayo","Location":"London, UK","Industry_Hint":"public risk compliance"},
  {"FirstName":"Vivian","LastName":"Chen","Designation":"Senior Manager, FP&A","Company":"Meridian Corp Inc.","LinkedInURL":"https://linkedin.com/in/vivianchen","Location":"Shanghai, China","Industry_Hint":"public finance fpa"},
  {"FirstName":"Patrick","LastName":"Lowe","Designation":"Manager, IT Infrastructure","Company":"Meridian Corp Inc.","LinkedInURL":"https://linkedin.com/in/patricklowe","Location":"Sydney, Australia","Industry_Hint":"public it infrastructure"},
  {"FirstName":"Ekta","LastName":"Mishra","Designation":"Senior Software Engineer","Company":"Meridian Corp Inc.","LinkedInURL":"https://linkedin.com/in/ektamishra","Location":"Hyderabad, India","Industry_Hint":"public software engineering"},
  {"FirstName":"Peter","LastName":"Van den Berg","Designation":"Analyst, Market Intelligence","Company":"Meridian Corp Inc.","LinkedInURL":"https://linkedin.com/in/peterberg","Location":"Amsterdam, Netherlands","Industry_Hint":"public analytics"},
  {"FirstName":"Leah","LastName":"Morrison","Designation":"Junior Accountant","Company":"Meridian Corp Inc.","LinkedInURL":"https://linkedin.com/in/leahmorrison","Location":"Toronto, Canada","Industry_Hint":"public accounting finance"},
  {"FirstName":"Chidi","LastName":"Nwosu","Designation":"Graduate Trainee, Operations","Company":"Meridian Corp Inc.","LinkedInURL":"https://linkedin.com/in/chidinwosu","Location":"Lagos, Nigeria","Industry_Hint":"public operations"},
  {"FirstName":"Svetlana","LastName":"Morozova","Designation":"CISO","Company":"Meridian Corp Inc.","LinkedInURL":"https://linkedin.com/in/svetlanamorozova","Location":"Moscow, Russia","Industry_Hint":"public security"},
  {"FirstName":"George","LastName":"Papadopoulos","Designation":"Regional Head, EMEA","Company":"Meridian Corp Inc.","LinkedInURL":"https://linkedin.com/in/georgepapadopoulos","Location":"Athens, Greece","Industry_Hint":"public"},

  # ── PRIVATE (15) ───────────────────────────────────────
  {"FirstName":"Danielle","LastName":"Marchand","Designation":"Managing Director","Company":"Marchand & Partners LLP","LinkedInURL":"https://linkedin.com/in/daniellemarchand","Location":"Toronto, Canada","Industry_Hint":"private consulting"},
  {"FirstName":"Olga","LastName":"Petersen","Designation":"Partner, Strategy","Company":"Nordic Advisors","LinkedInURL":"https://linkedin.com/in/olgapetersen","Location":"Copenhagen, Denmark","Industry_Hint":"private strategy"},
  {"FirstName":"Samuel","LastName":"Osei","Designation":"Senior Consultant","Company":"Ghana Advisory Group","LinkedInURL":"https://linkedin.com/in/samuelosei","Location":"Accra, Ghana","Industry_Hint":"private consulting"},
  {"FirstName":"Beatriz","LastName":"Carvalho","Designation":"Head of Research","Company":"BrasilTech Private","LinkedInURL":"https://linkedin.com/in/beatrizcarvalho","Location":"São Paulo, Brazil","Industry_Hint":"private research"},
  {"FirstName":"Vikram","LastName":"Bose","Designation":"Director, Business Development","Company":"Bose Holdings Pvt Ltd","LinkedInURL":"https://linkedin.com/in/vikrambose","Location":"Mumbai, India","Industry_Hint":"private business development"},
  {"FirstName":"Rachel","LastName":"Nguyen","Designation":"Content Strategist","Company":"Creative Co Private","LinkedInURL":"https://linkedin.com/in/rachelnguyen","Location":"Ho Chi Minh City, Vietnam","Industry_Hint":"private content marketing"},
  {"FirstName":"Santiago","LastName":"Reyes","Designation":"Junior Sales Executive","Company":"Reyes Trading","LinkedInURL":"https://linkedin.com/in/santiagoreyes","Location":"Buenos Aires, Argentina","Industry_Hint":"private sales"},
  {"FirstName":"Fumiko","LastName":"Watanabe","Designation":"VP, Operations","Company":"Watanabe Group","LinkedInURL":"https://linkedin.com/in/fumikow","Location":"Osaka, Japan","Industry_Hint":"private operations"},
  {"FirstName":"Blessing","LastName":"Obi","Designation":"HR Business Partner","Company":"Lagos Ventures","LinkedInURL":"https://linkedin.com/in/blessingobi","Location":"Lagos, Nigeria","Industry_Hint":"private hr"},
  {"FirstName":"Igor","LastName":"Klimov","Designation":"Senior Engineer, Platform","Company":"Klimov Tech","LinkedInURL":"https://linkedin.com/in/igorklimov","Location":"Kyiv, Ukraine","Industry_Hint":"private platform software"},
  {"FirstName":"Caitlin","LastName":"Brooks","Designation":"Marketing Manager","Company":"Brooks Media","LinkedInURL":"https://linkedin.com/in/caitlinbrooks","Location":"Auckland, New Zealand","Industry_Hint":"private marketing"},
  {"FirstName":"Ravi","LastName":"Iyer","Designation":"Founder & CEO","Company":"Iyer Innovations","LinkedInURL":"https://linkedin.com/in/raviiyer","Location":"Chennai, India","Industry_Hint":"private startup"},
  {"FirstName":"Emeka","LastName":"Eze","Designation":"Apprentice, Finance","Company":"Abuja Finance House","LinkedInURL":"https://linkedin.com/in/emekaeze","Location":"Abuja, Nigeria","Industry_Hint":"private finance"},
  {"FirstName":"Sophie","LastName":"Laurent","Designation":"Associate Director, M&A","Company":"Laurent Capital","LinkedInURL":"https://linkedin.com/in/sophielaurent","Location":"Luxembourg","Industry_Hint":"private finance"},
  {"FirstName":"Nnamdi","LastName":"Okafor","Designation":"Board Member","Company":"West Africa Holdings","LinkedInURL":"https://linkedin.com/in/nnamdiokafor","Location":"Lagos, Nigeria","Industry_Hint":"private"},
]

out_path = Path(__file__).parent / "test_data.json"
with open(out_path, "w") as f:
    json.dump(RECORDS, f, indent=2)

print(f"Generated {len(RECORDS)} records → {out_path}")
