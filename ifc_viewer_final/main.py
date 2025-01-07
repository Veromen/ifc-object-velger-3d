import ifcopenshell
import ifcopenshell.api
from typing import Union
import streamlit as st
import tempfile
import logging
import os
import zipfile
import base64
import subprocess
import uuid
import textwrap
import requests
import zipfile
from pathlib import Path

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("IFCLogger")

@st.cache_resource
def install_ifcconvert():
    """
    Downloads and installs the ifcconvert binary if it's not already present.
    Returns the absolute path to the ifcconvert binary.
    """
    ifcconvert_path = Path("/tmp/IfcConvert")
    
    if not ifcconvert_path.exists():
        st.write("🔄 Downloading ifcconvert...")
        url = "https://github.com/IfcOpenShell/IfcOpenShell/releases/download/ifcconvert-0.8.0/ifcconvert-0.8.0-linux64.zip"
        zip_path = "/tmp/ifcconvert.zip"
        
        try:
            # Download the zip file
            with requests.get(url, stream=True) as response:
                response.raise_for_status()
                with open(zip_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
            st.write("✅ Download complete.")
            
            # Extract the IfcConvert binary
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall("/tmp/")
            st.write("✅ Extraction complete.")
            
            # Remove the zip file
            os.remove(zip_path)
            
            # Make the binary executable
            ifcconvert_path.chmod(0o755)
            st.write("✅ ifcconvert is ready to use.")
            
        except Exception as e:
            st.error(f"🚨 Failed to install ifcconvert: {e}")
            st.stop()
    
    return str(ifcconvert_path)

class Patcher:
    def __init__(
            self,
            file: ifcopenshell.file,
            logger: logging.Logger,
            stories: list[str],
            keywords: list[str],
            ifc_product: Union[str, None],
            filter_option: str
    ):
        self.file = file
        self.logger = logger
        self.stories = stories
        self.keywords = keywords
        self.ifc_product = ifc_product
        self.filter_option = filter_option

    def patch(self):
        self.contained_ins: dict[str, set[ifcopenshell.entity_instance]] = {}
        self.aggregates: dict[str, set[ifcopenshell.entity_instance]] = {}
        self.new = ifcopenshell.file(schema=self.file.schema)
        self.owner_history = None
        self.reuse_identities: dict[int, ifcopenshell.entity_instance] = {}

        for owner_history in self.file.by_type("IfcOwnerHistory"):
            self.owner_history = self.new.add(owner_history)
            break

        self.add_element(self.file.by_type("IfcProject")[0])

        for element in self.filter_elements():
            self.add_element(element)

        self.create_spatial_tree()

        self.file = self.new

    def filter_elements(self):
        elements = self.file.by_type("IfcProduct")
        filtered_elements = []
        for element in elements:
            if self.filter_option == "IFC Product and Keywords":
                if element.is_a(self.ifc_product) and (
                        not self.keywords
                        or any(keyword.lower() in (element.Name or "").lower() for keyword in self.keywords)
                ):
                    if any(
                            story in [rel.RelatingStructure.Name for rel in
                                      getattr(element, "ContainedInStructure", [])]
                            for story in self.stories
                    ):
                        filtered_elements.append(element)
            elif self.filter_option == "Keywords Only":
                if any(keyword.lower() in (element.Name or "").lower() for keyword in self.keywords):
                    if any(
                            story in [rel.RelatingStructure.Name for rel in
                                      getattr(element, "ContainedInStructure", [])]
                            for story in self.stories
                    ):
                        filtered_elements.append(element)
        return filtered_elements

    def add_element(self, element: ifcopenshell.entity_instance) -> None:
        new_element = self.append_asset(element)
        if not new_element:
            return
        self.add_spatial_structures(element, new_element)
        self.add_decomposition_parents(element, new_element)

    def append_asset(self, element: ifcopenshell.entity_instance) -> Union[ifcopenshell.entity_instance, None]:
        try:
            return self.new.by_guid(element.GlobalId)
        except:
            pass
        if element.is_a("IfcProject"):
            return self.new.add(element)
        return ifcopenshell.api.run(
            "project.append_asset",
            self.new,
            library=self.file,
            element=element,
            reuse_identities=self.reuse_identities
        )

    def add_spatial_structures(self, element: ifcopenshell.entity_instance,
                               new_element: ifcopenshell.entity_instance) -> None:
        for rel in getattr(element, "ContainedInStructure", []):
            spatial_element = rel.RelatingStructure
            new_spatial_element = self.append_asset(spatial_element)
            self.contained_ins.setdefault(spatial_element.GlobalId, set()).add(new_element)
            self.add_decomposition_parents(spatial_element, new_spatial_element)

    def add_decomposition_parents(self, element: ifcopenshell.entity_instance,
                                  new_element: ifcopenshell.entity_instance) -> None:
        for rel in getattr(element, "Decomposes", []):
            parent = rel.RelatingObject
            new_parent = self.append_asset(parent)
            self.aggregates.setdefault(parent.GlobalId, set()).add(new_element)
            self.add_decomposition_parents(parent, new_parent)
            self.add_spatial_structures(parent, new_parent)

    def create_spatial_tree(self) -> None:
        for relating_structure_guid, related_elements in self.contained_ins.items():
            self.new.createIfcRelContainedInSpatialStructure(
                ifcopenshell.guid.new(),
                self.owner_history,
                None,
                None,
                list(related_elements),
                self.new.by_guid(relating_structure_guid),
            )
        for relating_object_guid, related_objects in self.aggregates.items():
            self.new.createIfcRelAggregates(
                ifcopenshell.guid.new(),
                self.owner_history,
                None,
                None,
                self.new.by_guid(relating_object_guid),
                list(related_objects),
            )

def main():
    st.title("🛠️ IFC Filtering and Conversion App")

    # Initialize session state variables
    if 'filtered_ifc_data' not in st.session_state:
        st.session_state.filtered_ifc_data = None
    if 'output_filename' not in st.session_state:
        st.session_state.output_filename = ""
    if 'patcher' not in st.session_state:
        st.session_state.patcher = None
    if 'file_bytes' not in st.session_state:
        st.session_state.file_bytes = None
    if 'uploaded_file_name' not in st.session_state:
        st.session_state.uploaded_file_name = ""
    if 'stories' not in st.session_state:
        st.session_state.stories = []
    if 'keywords' not in st.session_state:
        st.session_state.keywords = []
    if 'filter_option' not in st.session_state:
        st.session_state.filter_option = ""
    if 'ifc_product' not in st.session_state:
        st.session_state.ifc_product = None
    if 'ifcconvert_path' not in st.session_state:
        st.session_state.ifcconvert_path = None

    # Step 1: Install ifcconvert
    if st.session_state.ifcconvert_path is None:
        st.session_state.ifcconvert_path = install_ifcconvert()

    def filter_ifc_callback():
        if st.session_state.file_bytes is None:
            st.error("No file uploaded.")
            return

        # Save the uploaded file to a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ifc") as tmp_file:
            tmp_file.write(st.session_state.file_bytes)
            tmp_file_path = tmp_file.name

        # Handle IFCZIP files
        if st.session_state.uploaded_file_name.endswith('.ifczip'):
            try:
                with zipfile.ZipFile(tmp_file_path, 'r') as zip_ref:
                    extract_dir = tempfile.mkdtemp()
                    zip_ref.extractall(extract_dir)
                    extracted_files = zip_ref.namelist()
                    ifc_files = [f for f in extracted_files if f.endswith(".ifc")]
                    if ifc_files:
                        tmp_file_path = os.path.join(extract_dir, ifc_files[0])
                    else:
                        st.error("No IFC files found in the uploaded IFCZIP.")
                        return
            except zipfile.BadZipFile:
                st.error("Uploaded file is not a valid zip archive.")
                return
            except Exception as e:
                st.error(f"Error extracting IFCZIP file: {e}")
                return

        try:
            file = ifcopenshell.open(tmp_file_path)
        except Exception as e:
            st.error(f"Failed to open IFC file: {e}")
            return

        # Initialize logger
        logger = logging.getLogger("IFCLogger")
        logger.setLevel(logging.DEBUG)
        if not logger.handlers:
            ch = logging.StreamHandler()
            ch.setLevel(logging.DEBUG)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            ch.setFormatter(formatter)
            logger.addHandler(ch)

        # Initialize and run the patcher
        patcher = Patcher(
            file=file,
            logger=logger,
            stories=st.session_state.stories,
            keywords=st.session_state.keywords,
            ifc_product=st.session_state.ifc_product,
            filter_option=st.session_state.filter_option
        )
        try:
            patcher.patch()
            st.session_state.patcher = patcher
            st.session_state.filtered_ifc_data = patcher.file.to_string()
            st.session_state.output_filename = st.session_state.output_filename or f"filtered_{st.session_state.uploaded_file_name}"
        except Exception as e:
            st.error(f"Error during filtering: {e}")

    def reset_filter_callback():
        st.session_state.filtered_ifc_data = None
        st.session_state.output_filename = ""
        st.session_state.patcher = None
        st.session_state.file_bytes = None
        st.session_state.uploaded_file_name = ""
        st.session_state.stories = []
        st.session_state.keywords = []
        st.session_state.filter_option = ""
        st.session_state.ifc_product = None

    if st.session_state.filtered_ifc_data is None:
        uploaded_file = st.file_uploader("🔽 Choose an IFC or IFCZIP file", type=["ifc", "ifczip"])
        if uploaded_file is not None:
            st.session_state.file_bytes = uploaded_file.read()
            st.session_state.uploaded_file_name = uploaded_file.name
            st.header("📋 Filter Options")

            # Handle file extraction if IFCZIP
            with tempfile.NamedTemporaryFile(delete=False, suffix=".ifc") as tmp_file:
                tmp_file.write(st.session_state.file_bytes)
                tmp_file_path = tmp_file.name

            if uploaded_file.name.endswith('.ifczip'):
                try:
                    with zipfile.ZipFile(tmp_file_path, 'r') as zip_ref:
                        extract_dir = tempfile.mkdtemp()
                        zip_ref.extractall(extract_dir)
                        extracted_files = zip_ref.namelist()
                        ifc_files = [f for f in extracted_files if f.endswith(".ifc")]
                        if ifc_files:
                            tmp_file_path = os.path.join(extract_dir, ifc_files[0])
                        else:
                            st.error("No IFC files found in the uploaded IFCZIP.")
                            st.stop()
                except zipfile.BadZipFile:
                    st.error("Uploaded file is not a valid zip archive.")
                    st.stop()
                except Exception as e:
                    st.error(f"Error extracting IFCZIP file: {e}")
                    st.stop()

            try:
                file = ifcopenshell.open(tmp_file_path)
            except Exception as e:
                st.error(f"Failed to open IFC file: {e}")
                st.stop()

            # Populate filter options
            stories_options = ["Keep All Stories"] + sorted({story.Name for story in file.by_type("IfcBuildingStorey") if story.Name})
            stories = st.multiselect("🔹 Select Stories to Keep", options=stories_options, default=["Keep All Stories"])
            if "Keep All Stories" in stories or not stories:
                stories = sorted({story.Name for story in file.by_type("IfcBuildingStorey") if story.Name})
            st.session_state.stories = stories

            filter_option = st.selectbox(
                "🔹 Choose Filtering Option",
                options=["IFC Product and Keywords", "Keywords Only"]
            )
            st.session_state.filter_option = filter_option

            if filter_option == "IFC Product and Keywords":
                ifc_products = sorted({entity.is_a() for entity in file.by_type("IfcProduct")})
                ifc_product = st.selectbox("🔹 Select IFC Product to Filter", options=ifc_products)
                st.session_state.ifc_product = ifc_product
            else:
                st.session_state.ifc_product = None

            keywords_input = st.text_input("🔹 Enter Keywords to Filter Elements (comma separated)")
            keywords = [kw.strip() for kw in keywords_input.split(',') if kw.strip()]
            st.session_state.keywords = keywords

            # Generate default output filename
            input_filename = os.path.splitext(uploaded_file.name)[0]
            suffix = "_stories_" + "_".join([s.replace(" ", "_") for s in stories])
            if filter_option == "IFC Product and Keywords" and st.session_state.ifc_product:
                suffix += f"_product_{st.session_state.ifc_product.replace(' ', '_')}"
            if keywords:
                suffix += "_keywords_" + "_".join([kw.replace(" ", "_") for kw in keywords])
            default_output_filename = f"{input_filename}{suffix}"
            output_filename = st.text_input("🔹 Output IFC Filename (optional)", value=default_output_filename)
            st.session_state.output_filename = output_filename

            # Filter Button
            st.button("🔄 Filter IFC Model", on_click=filter_ifc_callback)
    else:
        patcher = st.session_state.patcher
        filtered_ifc_data = st.session_state.filtered_ifc_data
        output_filename = st.session_state.output_filename
        if not patcher or not filtered_ifc_data:
            st.error("No filtered data available.")
        else:
            filtered_products = patcher.file.by_type("IfcProduct")
            if not filtered_products:
                st.error("No objects found matching the given criteria.")
            else:
                st.header(f"📁 Filtered IFC: {st.session_state.uploaded_file_name}")
                st.write(f"**Stories:** {', '.join(st.session_state.stories)}")
                if st.session_state.ifc_product:
                    st.write(f"**IFC Product:** {st.session_state.ifc_product}")
                st.write(f"**Keywords:** {', '.join(st.session_state.keywords)}")

                # Ensure output filename has .ifc extension
                if not output_filename.lower().endswith(".ifc"):
                    output_filename += ".ifc"

                # Download Button for Filtered IFC
                st.download_button(
                    "📥 Download Filtered IFC",
                    data=filtered_ifc_data,
                    file_name=output_filename
                )

                # Conversion to GLB
                unique_id = uuid.uuid4().hex
                glb_filename = f"filtered_model_{unique_id}.glb"
                with tempfile.NamedTemporaryFile(suffix=".ifc", delete=False) as filtered_ifc:
                    filtered_ifc.write(filtered_ifc_data.encode("utf-8"))
                    filtered_ifc_path = filtered_ifc.name

                glb_path = os.path.join(tempfile.gettempdir(), glb_filename)
                
                # Construct the absolute path to ifcconvert
                ifcconvert_path = st.session_state.ifcconvert_path

                # Define the conversion command using the absolute path
                convert_cmd = f'"{ifcconvert_path}" "{filtered_ifc_path}" "{glb_path}"'

                # Run the conversion
                with st.spinner("🔄 Converting IFC to GLB..."):
                    retcode = subprocess.run(
                        convert_cmd,
                        shell=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )

                if retcode.returncode != 0:
                    st.error("🚨 Conversion to GLB failed. Ensure ifcconvert is installed correctly.")
                    st.error(retcode.stderr.decode())
                else:
                    try:
                        with open(glb_path, "rb") as f:
                            glb_content = f.read()
                        glb_base64 = base64.b64encode(glb_content).decode("utf-8")
                    except Exception as e:
                        st.error(f"Error reading GLB file: {e}")
                        return

                    # Embed 3D Viewer using Three.js
                    html_snippet = textwrap.dedent(f"""
                        <!DOCTYPE html>
                        <html>
                        <head>
                          <meta charset="utf-8" />
                          <title>3D Viewer (Double-Click to Enlarge)</title>
                          <style>
                            body {{
                              margin: 0;
                              padding: 0;
                            }}
                            #viewer-container {{
                              width: 100%;
                              height: 100%;
                              position: absolute;
                              top: 0;
                              left: 0;
                              overflow: hidden;
                              cursor: pointer;
                            }}
                            .fullscreen {{
                              position: fixed !important;
                              top: 0 !important;
                              left: 0 !important;
                              width: 100% !important;
                              height: 100% !important;
                              z-index: 9999 !important;
                            }}
                            #fullscreen-comment {{
                              position: absolute;
                              bottom: 10px;
                              width: 100%;
                              text-align: center;
                              color: #555;
                              background: rgba(255, 255, 255, 0.7);
                              padding: 5px 0;
                              pointer-events: none;
                            }}
                          </style>
                        </head>
                        <body>
                          <div id="viewer-container"></div>
                          <div id="fullscreen-comment">Double click 3D window for fullscreen</div>
                
                          <script type="importmap">
                          {{
                            "imports": {{
                              "three": "https://cdn.jsdelivr.net/npm/three@0.155.0/build/three.module.js"
                            }}
                          }}
                          </script>
                
                          <script type="module">
                            import * as THREE from 'three';
                            import {{ OrbitControls }} from 'https://cdn.jsdelivr.net/npm/three@0.155.0/examples/jsm/controls/OrbitControls.js';
                            import {{ GLTFLoader }} from 'https://cdn.jsdelivr.net/npm/three@0.155.0/examples/jsm/loaders/GLTFLoader.js';
                
                            const container = document.getElementById('viewer-container');
                            const scene = new THREE.Scene();
                            scene.background = new THREE.Color(0xdddddd);
                
                            const camera = new THREE.PerspectiveCamera(
                              75,
                              window.innerWidth / window.innerHeight,
                              0.1,
                              1000
                            );
                            camera.position.set(0, 3, 10);
                
                            const renderer = new THREE.WebGLRenderer({{ antialias: true }});
                            renderer.setSize(window.innerWidth, window.innerHeight);
                            container.appendChild(renderer.domElement);
                
                            const controls = new OrbitControls(camera, renderer.domElement);
                            controls.enableDamping = true;
                            controls.dampingFactor = 0.05;
                
                            const hemiLight = new THREE.HemisphereLight(0xffffff, 0x444444, 1);
                            scene.add(hemiLight);
                            const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
                            dirLight.position.set(0, 20, 10);
                            scene.add(dirLight);
                
                            const base64Data = "{glb_base64}";
                            const binary = atob(base64Data);
                            const array = new Uint8Array(binary.length);
                            for (let i = 0; i < binary.length; i++) {{
                              array[i] = binary.charCodeAt(i);
                            }}
                            const blob = new Blob([array], {{ type: 'model/gltf-binary' }});
                            const glbUrl = URL.createObjectURL(blob);
                
                            const loader = new GLTFLoader();
                            loader.load(
                              glbUrl,
                              (gltf) => {{
                                console.log("GLB model loaded successfully.");
                                scene.add(gltf.scene);
                
                                const box = new THREE.Box3().setFromObject(gltf.scene);
                                const center = box.getCenter(new THREE.Vector3());
                                const size = box.getSize(new THREE.Vector3());
                
                                const maxDim = Math.max(size.x, size.y, size.z);
                                const fov = camera.fov * (Math.PI / 180);
                                let cameraZ = Math.abs(maxDim / 2 * Math.tan(fov * 2));
                
                                camera.position.z = cameraZ * 2;
                                camera.lookAt(center);
                
                                controls.target.copy(center);
                                controls.update();
                
                                animate();
                              }},
                              (xhr) => {{
                                console.log((xhr.loaded / xhr.total * 100) + '% loaded');
                              }},
                              (error) => {{
                                console.error("Error loading GLB:", error);
                              }}
                            );
                
                            window.addEventListener('resize', onWindowResize, false);
                            function onWindowResize() {{
                              camera.aspect = window.innerWidth / window.innerHeight;
                              camera.updateProjectionMatrix();
                              renderer.setSize(window.innerWidth, window.innerHeight);
                            }}
                
                            container.addEventListener('dblclick', () => {{
                              if (!document.fullscreenElement) {{
                                container.requestFullscreen().catch(err => {{
                                  console.error(`Error attempting to enable full-screen mode: ${{err.message}} (${{err.name}})`);
                                }});
                              }} else {{
                                document.exitFullscreen();
                              }}
                            }});
                
                            function animate() {{
                              requestAnimationFrame(animate);
                              controls.update();
                              renderer.render(scene, camera);
                            }}
                          </script>
                        </body>
                        </html>
                    """)

                    # Replace placeholder with actual base64 GLB data
                    html_snippet = html_snippet.replace("{glb_base64}", glb_base64)

                    # Embed the 3D viewer
                    st.markdown("### 📊 3D Model Preview")
                    st.components.v1.html(html_snippet, height=600, scrolling=False)
        
        # Reset Button
        st.button("🔄 Filter New IFC Model", on_click=reset_filter_callback)

if __name__ == "__main__":
    main()
