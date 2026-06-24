/**
 * CDN Image Widget
 * Admin widget for CdnImageField and CdnImageListField
 *
 * Provides:
 * - Preview for images
 * - Asset picker for catalog/carousel types
 * - Random image button for product/chat/avatar types
 * - Carousel navigation for image lists
 */
(function() {
    'use strict';

    const CDN_MEDIA_URL = '/media/cdn/';
    const CDN_API_BASE = '/cdn/api/';

    // Asset types use picker, image types use random button
    const ASSET_TYPES = ['catalog', 'carousel'];
    const IMAGE_TYPES = ['product', 'chat', 'avatar', 'review'];

    // Cache for asset lists
    const assetCache = {};

    /**
     * Fetch assets list from CDN
     */
    async function fetchAssets(assetType) {
        if (assetCache[assetType]) {
            return assetCache[assetType];
        }
        try {
            const response = await fetch(`${CDN_API_BASE}assets/?type=${assetType}`, {
                credentials: 'include'
            });
            if (!response.ok) throw new Error('Failed to fetch assets');
            const data = await response.json();
            assetCache[assetType] = data.names || [];
            return assetCache[assetType];
        } catch (error) {
            console.error('Error fetching assets:', error);
            return [];
        }
    }

    /**
     * Fetch random image from CDN
     */
    async function fetchRandomImage(imageType) {
        try {
            const response = await fetch(`${CDN_API_BASE}images/${imageType}/random/`, {
                credentials: 'include'
            });
            if (!response.ok) {
                if (response.status === 404) {
                    alert(`No ${imageType} images found in CDN`);
                    return null;
                }
                throw new Error('Failed to fetch random image');
            }
            const data = await response.json();
            return data.file_hash;
        } catch (error) {
            console.error('Error fetching random image:', error);
            alert('Failed to fetch random image');
            return null;
        }
    }

    /**
     * Upload image to CDN
     */
    async function uploadImage(imageType, file) {
        try {
            const formData = new FormData();
            formData.append('file', file);

            const response = await fetch(`${CDN_API_BASE}images/${imageType}/upload/`, {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'X-CSRFToken': getCsrfToken()
                },
                body: formData
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Upload failed');
            }

            const data = await response.json();
            return data.image.file_hash;
        } catch (error) {
            console.error('Error uploading image:', error);
            alert(`Upload failed: ${error.message}`);
            return null;
        }
    }

    /**
     * Upload asset to CDN
     */
    async function uploadAsset(assetType, file) {
        // Prompt for asset name
        const fileName = file.name.replace(/\.[^/.]+$/, ''); // Remove extension
        const name = prompt('Enter asset name (letters, numbers, underscores, hyphens):', fileName.replace(/[^a-zA-Z0-9_-]/g, '-'));
        if (!name) return null;

        try {
            const formData = new FormData();
            formData.append('file', file);
            formData.append('name', name);
            formData.append('type', assetType);

            const response = await fetch(`${CDN_API_BASE}upload/asset/`, {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'X-CSRFToken': getCsrfToken()
                },
                body: formData
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Upload failed');
            }

            const data = await response.json();
            return data.asset.name;
        } catch (error) {
            console.error('Error uploading asset:', error);
            alert(`Upload failed: ${error.message}`);
            return null;
        }
    }

    /**
     * Get CSRF token from various sources
     */
    function getCsrfToken() {
        // 1. Try hidden input field (Django admin forms)
        const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input) {
            return input.value;
        }

        // 2. Try meta tag
        const meta = document.querySelector('meta[name="csrf-token"]');
        if (meta) {
            return meta.getAttribute('content');
        }

        // 3. Try cookies
        const name = 'csrftoken';
        const cookies = document.cookie.split(';');
        for (let cookie of cookies) {
            cookie = cookie.trim();
            if (cookie.startsWith(name + '=')) {
                return cookie.substring(name.length + 1);
            }
        }
        return '';
    }

    /**
     * Get preview URL for a CDN reference
     */
    function getPreviewUrl(reference, size = '64') {
        if (!reference) return null;
        const [type, id] = reference.split('/');
        return `${CDN_MEDIA_URL}${type}/${id}/${size}.webp`;
    }

    /**
     * Parse CDN reference from input value
     */
    function parseReference(value) {
        if (!value || !value.includes('/')) return { type: null, id: null };
        const [type, id] = value.split('/', 2);
        return { type, id };
    }

    /**
     * Build CDN reference string
     */
    function buildReference(type, id) {
        return `${type}/${id}`;
    }

    /**
     * Initialize single image field widget
     */
    function initCdnImageField(input) {
        if (input.dataset.cdnWidgetInit) return;
        input.dataset.cdnWidgetInit = 'true';

        const imageType = input.dataset.cdnImageType;
        const isAsset = input.dataset.cdnIsAsset === 'true';

        // Create wrapper
        const wrapper = document.createElement('div');
        wrapper.className = 'cdn-image-widget';
        input.parentNode.insertBefore(wrapper, input);

        // Create preview container
        const preview = document.createElement('div');
        preview.className = 'cdn-image-preview';
        wrapper.appendChild(preview);

        // Create input container
        const inputContainer = document.createElement('div');
        inputContainer.className = 'cdn-image-input-container';
        wrapper.appendChild(inputContainer);

        // Move input into container
        inputContainer.appendChild(input);

        // Create buttons container
        const buttons = document.createElement('div');
        buttons.className = 'cdn-image-buttons';
        inputContainer.appendChild(buttons);

        // Create clear button
        const clearBtn = document.createElement('button');
        clearBtn.type = 'button';
        clearBtn.className = 'cdn-image-btn cdn-image-clear';
        clearBtn.textContent = 'Clear';
        clearBtn.addEventListener('click', () => {
            input.value = '';
            updatePreview();
        });
        buttons.appendChild(clearBtn);

        if (isAsset) {
            // Asset picker dropdown
            const dropdown = document.createElement('div');
            dropdown.className = 'cdn-asset-dropdown';
            document.body.appendChild(dropdown);

            let assets = [];
            let isDropdownVisible = false;

            fetchAssets(imageType).then(data => { assets = data; });

            function positionDropdown() {
                const rect = input.getBoundingClientRect();
                dropdown.style.top = (rect.bottom + 2) + 'px';
                dropdown.style.left = rect.left + 'px';
                dropdown.style.width = Math.max(rect.width, 300) + 'px';
            }

            function showDropdown(filter = '') {
                const filterLower = filter.toLowerCase();
                const filtered = assets.filter(name =>
                    name.toLowerCase().includes(filterLower)
                );

                if (filtered.length === 0) {
                    dropdown.innerHTML = '<div class="cdn-asset-item no-results">No assets found</div>';
                } else {
                    dropdown.innerHTML = filtered.map(name => `
                        <div class="cdn-asset-item" data-value="${name}">
                            <img src="${getPreviewUrl(buildReference(imageType, name))}" alt="" onerror="this.style.display='none'">
                            <span>${name}</span>
                        </div>
                    `).join('');
                }
                positionDropdown();
                dropdown.style.display = 'block';
                isDropdownVisible = true;
            }

            function hideDropdown() {
                dropdown.style.display = 'none';
                isDropdownVisible = false;
            }

            function selectAsset(name) {
                input.value = buildReference(imageType, name);
                updatePreview();
                hideDropdown();
            }

            input.addEventListener('focus', () => showDropdown(parseReference(input.value).id || ''));
            input.addEventListener('input', () => {
                const { id } = parseReference(input.value);
                showDropdown(id || input.value);
                updatePreview();
            });

            dropdown.addEventListener('click', (e) => {
                const item = e.target.closest('.cdn-asset-item');
                if (item && item.dataset.value) {
                    selectAsset(item.dataset.value);
                }
            });

            document.addEventListener('click', (e) => {
                if (!wrapper.contains(e.target) && !dropdown.contains(e.target)) {
                    hideDropdown();
                }
            });

            window.addEventListener('scroll', () => {
                if (isDropdownVisible) positionDropdown();
            }, true);

            input.addEventListener('keydown', (e) => {
                if (!isDropdownVisible) return;
                const items = dropdown.querySelectorAll('.cdn-asset-item[data-value]');
                const current = dropdown.querySelector('.cdn-asset-item.selected');
                let index = Array.from(items).indexOf(current);

                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    if (current) current.classList.remove('selected');
                    index = (index + 1) % items.length;
                    items[index]?.classList.add('selected');
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    if (current) current.classList.remove('selected');
                    index = index <= 0 ? items.length - 1 : index - 1;
                    items[index]?.classList.add('selected');
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    if (current && current.dataset.value) selectAsset(current.dataset.value);
                } else if (e.key === 'Escape') {
                    hideDropdown();
                }
            });

            // Upload button for asset types
            const assetFileInput = document.createElement('input');
            assetFileInput.type = 'file';
            assetFileInput.accept = 'image/*';
            assetFileInput.style.display = 'none';
            wrapper.appendChild(assetFileInput);

            const assetUploadBtn = document.createElement('button');
            assetUploadBtn.type = 'button';
            assetUploadBtn.className = 'cdn-image-btn cdn-image-upload';
            assetUploadBtn.textContent = 'Upload';
            assetUploadBtn.addEventListener('click', () => assetFileInput.click());
            buttons.appendChild(assetUploadBtn);

            assetFileInput.addEventListener('change', async () => {
                if (!assetFileInput.files || !assetFileInput.files[0]) return;
                assetUploadBtn.disabled = true;
                assetUploadBtn.textContent = 'Uploading...';
                const name = await uploadAsset(imageType, assetFileInput.files[0]);
                if (name) {
                    input.value = buildReference(imageType, name);
                    updatePreview();
                    // Refresh asset list
                    assets = await fetchAssets(imageType);
                }
                assetUploadBtn.disabled = false;
                assetUploadBtn.textContent = 'Upload';
                assetFileInput.value = '';
            });
        } else {
            // Upload button for image types
            const fileInput = document.createElement('input');
            fileInput.type = 'file';
            fileInput.accept = 'image/*';
            fileInput.style.display = 'none';
            wrapper.appendChild(fileInput);

            const uploadBtn = document.createElement('button');
            uploadBtn.type = 'button';
            uploadBtn.className = 'cdn-image-btn cdn-image-upload';
            uploadBtn.textContent = 'Upload';
            uploadBtn.addEventListener('click', () => fileInput.click());
            buttons.appendChild(uploadBtn);

            fileInput.addEventListener('change', async () => {
                if (!fileInput.files || !fileInput.files[0]) return;
                uploadBtn.disabled = true;
                uploadBtn.textContent = 'Uploading...';
                const hash = await uploadImage(imageType, fileInput.files[0]);
                if (hash) {
                    input.value = buildReference(imageType, hash);
                    updatePreview();
                }
                uploadBtn.disabled = false;
                uploadBtn.textContent = 'Upload';
                fileInput.value = '';
            });

            // Random image button for image types
            const randomBtn = document.createElement('button');
            randomBtn.type = 'button';
            randomBtn.className = 'cdn-image-btn cdn-image-random';
            randomBtn.textContent = 'Random';
            randomBtn.addEventListener('click', async () => {
                randomBtn.disabled = true;
                randomBtn.textContent = '...';
                const hash = await fetchRandomImage(imageType);
                if (hash) {
                    input.value = buildReference(imageType, hash);
                    updatePreview();
                }
                randomBtn.disabled = false;
                randomBtn.textContent = 'Random';
            });
            buttons.appendChild(randomBtn);
        }

        function updatePreview() {
            const url = getPreviewUrl(input.value, '120');
            if (url && input.value) {
                preview.innerHTML = `<img src="${url}" alt="Preview" onerror="this.innerHTML='No preview'">`;
            } else {
                preview.innerHTML = '<span class="cdn-image-no-preview">No image</span>';
            }
        }

        // Initial preview
        updatePreview();
    }

    /**
     * Initialize image list field widget
     */
    function initCdnImageListField(input) {
        if (input.dataset.cdnWidgetInit) return;
        input.dataset.cdnWidgetInit = 'true';

        const imageType = input.dataset.cdnImageType;
        const isAsset = input.dataset.cdnIsAsset === 'true';
        const maxImages = parseInt(input.dataset.cdnMaxImages) || 9;

        // Parse current value
        let images = [];
        try {
            const val = input.value ? JSON.parse(input.value) : [];
            if (Array.isArray(val)) images = val;
        } catch (e) {
            images = [];
        }

        // Create wrapper
        const wrapper = document.createElement('div');
        wrapper.className = 'cdn-image-list-widget';
        input.parentNode.insertBefore(wrapper, input);
        input.style.display = 'none';

        // Items container
        const itemsContainer = document.createElement('div');
        itemsContainer.className = 'cdn-list-items';
        wrapper.appendChild(itemsContainer);

        // Add buttons container
        const addContainer = document.createElement('div');
        addContainer.className = 'cdn-list-add-container';
        wrapper.appendChild(addContainer);

        // Counter
        const counter = document.createElement('div');
        counter.className = 'cdn-list-counter';
        addContainer.appendChild(counter);

        // Asset dropdown (shared)
        let dropdown = null;
        let assets = [];
        let currentDropdownTarget = null;
        if (isAsset) {
            dropdown = document.createElement('div');
            dropdown.className = 'cdn-asset-dropdown';
            document.body.appendChild(dropdown);
            fetchAssets(imageType).then(data => { assets = data; });
        }

        function syncToInput() {
            input.value = JSON.stringify(images);
            input.dispatchEvent(new Event('change', { bubbles: true }));
        }

        function createItemWidget(index) {
            const item = document.createElement('div');
            item.className = 'cdn-list-item';
            item.dataset.index = index;

            // Preview
            const preview = document.createElement('div');
            preview.className = 'cdn-list-item-preview';
            item.appendChild(preview);

            // Input container
            const inputContainer = document.createElement('div');
            inputContainer.className = 'cdn-list-item-input-container';
            item.appendChild(inputContainer);

            // Item input
            const itemInput = document.createElement('input');
            itemInput.type = 'text';
            itemInput.className = 'cdn-list-item-input';
            itemInput.value = images[index] || '';
            itemInput.placeholder = isAsset ? 'Select asset...' : 'Enter reference or upload...';
            inputContainer.appendChild(itemInput);

            // Item buttons
            const itemButtons = document.createElement('div');
            itemButtons.className = 'cdn-list-item-buttons';
            inputContainer.appendChild(itemButtons);

            if (isAsset) {
                // Asset picker button
                const pickerBtn = document.createElement('button');
                pickerBtn.type = 'button';
                pickerBtn.className = 'cdn-image-btn cdn-image-picker';
                pickerBtn.textContent = 'Pick';
                pickerBtn.addEventListener('click', () => showAssetDropdown(index, pickerBtn));
                itemButtons.appendChild(pickerBtn);

                // Asset upload button
                const assetFileInput = document.createElement('input');
                assetFileInput.type = 'file';
                assetFileInput.accept = 'image/*';
                assetFileInput.style.display = 'none';
                item.appendChild(assetFileInput);

                const assetUploadBtn = document.createElement('button');
                assetUploadBtn.type = 'button';
                assetUploadBtn.className = 'cdn-image-btn cdn-image-upload';
                assetUploadBtn.textContent = 'Upload';
                assetUploadBtn.addEventListener('click', () => assetFileInput.click());
                itemButtons.appendChild(assetUploadBtn);

                assetFileInput.addEventListener('change', async () => {
                    if (!assetFileInput.files || !assetFileInput.files[0]) return;
                    assetUploadBtn.disabled = true;
                    assetUploadBtn.textContent = '...';
                    const name = await uploadAsset(imageType, assetFileInput.files[0]);
                    if (name) {
                        images[index] = buildReference(imageType, name);
                        assets = await fetchAssets(imageType);
                        renderItems();
                        syncToInput();
                    }
                    assetUploadBtn.disabled = false;
                    assetUploadBtn.textContent = 'Upload';
                    assetFileInput.value = '';
                });
            } else {
                // Image upload button
                const fileInput = document.createElement('input');
                fileInput.type = 'file';
                fileInput.accept = 'image/*';
                fileInput.style.display = 'none';
                item.appendChild(fileInput);

                const uploadBtn = document.createElement('button');
                uploadBtn.type = 'button';
                uploadBtn.className = 'cdn-image-btn cdn-image-upload';
                uploadBtn.textContent = 'Upload';
                uploadBtn.addEventListener('click', () => fileInput.click());
                itemButtons.appendChild(uploadBtn);

                fileInput.addEventListener('change', async () => {
                    if (!fileInput.files || !fileInput.files[0]) return;
                    uploadBtn.disabled = true;
                    uploadBtn.textContent = '...';
                    const hash = await uploadImage(imageType, fileInput.files[0]);
                    if (hash) {
                        images[index] = buildReference(imageType, hash);
                        renderItems();
                        syncToInput();
                    }
                    uploadBtn.disabled = false;
                    uploadBtn.textContent = 'Upload';
                    fileInput.value = '';
                });

                // Random button
                const randomBtn = document.createElement('button');
                randomBtn.type = 'button';
                randomBtn.className = 'cdn-image-btn cdn-image-random';
                randomBtn.textContent = 'Random';
                randomBtn.addEventListener('click', async () => {
                    randomBtn.disabled = true;
                    randomBtn.textContent = '...';
                    const hash = await fetchRandomImage(imageType);
                    if (hash) {
                        images[index] = buildReference(imageType, hash);
                        renderItems();
                        syncToInput();
                    }
                    randomBtn.disabled = false;
                    randomBtn.textContent = 'Random';
                });
                itemButtons.appendChild(randomBtn);
            }

            // Remove button
            const removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'cdn-image-btn cdn-image-clear';
            removeBtn.textContent = '×';
            removeBtn.title = 'Remove';
            removeBtn.addEventListener('click', () => {
                images.splice(index, 1);
                renderItems();
                syncToInput();
            });
            itemButtons.appendChild(removeBtn);

            // Update preview on input change
            itemInput.addEventListener('input', () => {
                images[index] = itemInput.value;
                updateItemPreview(preview, itemInput.value);
                syncToInput();
            });

            // Initial preview
            updateItemPreview(preview, images[index]);

            return item;
        }

        function updateItemPreview(preview, value) {
            const url = getPreviewUrl(value, '64');
            if (url && value) {
                preview.innerHTML = `<img src="${url}" alt="" onerror="this.outerHTML='<span class=\\'cdn-image-no-preview\\'>?</span>'">`;
            } else {
                preview.innerHTML = '<span class="cdn-image-no-preview">?</span>';
            }
        }

        function showAssetDropdown(index, btn) {
            currentDropdownTarget = { index, btn };
            const existing = new Set(images.filter((_, i) => i !== index).map(ref => parseReference(ref).id));
            const filtered = assets.filter(name => !existing.has(name));

            if (filtered.length === 0) {
                dropdown.innerHTML = '<div class="cdn-asset-item no-results">No assets available</div>';
            } else {
                dropdown.innerHTML = filtered.map(name => `
                    <div class="cdn-asset-item" data-value="${name}">
                        <img src="${getPreviewUrl(buildReference(imageType, name))}" alt="" onerror="this.style.display='none'">
                        <span>${name}</span>
                    </div>
                `).join('');
            }

            const rect = btn.getBoundingClientRect();
            dropdown.style.top = (rect.bottom + 2) + 'px';
            dropdown.style.left = rect.left + 'px';
            dropdown.style.width = '300px';
            dropdown.style.display = 'block';
        }

        if (isAsset && dropdown) {
            dropdown.addEventListener('click', (e) => {
                const item = e.target.closest('.cdn-asset-item');
                if (item && item.dataset.value && currentDropdownTarget) {
                    images[currentDropdownTarget.index] = buildReference(imageType, item.dataset.value);
                    renderItems();
                    syncToInput();
                    dropdown.style.display = 'none';
                    currentDropdownTarget = null;
                }
            });

            document.addEventListener('click', (e) => {
                if (!dropdown.contains(e.target) && !e.target.closest('.cdn-image-picker')) {
                    dropdown.style.display = 'none';
                    currentDropdownTarget = null;
                }
            });
        }

        function renderItems() {
            itemsContainer.innerHTML = '';
            for (let i = 0; i < images.length; i++) {
                itemsContainer.appendChild(createItemWidget(i));
            }
            updateCounter();
            updateAddButton();
        }

        function updateCounter() {
            counter.textContent = `${images.length} / ${maxImages}`;
        }

        function updateAddButton() {
            addBtn.disabled = images.length >= maxImages;
        }

        // Add new item button
        const addBtn = document.createElement('button');
        addBtn.type = 'button';
        addBtn.className = 'cdn-image-btn cdn-list-add-btn';
        addBtn.textContent = '+ Add';
        addBtn.addEventListener('click', () => {
            if (images.length < maxImages) {
                images.push('');
                renderItems();
                syncToInput();
            }
        });
        addContainer.appendChild(addBtn);

        // Initial render
        renderItems();
    }

    /**
     * Initialize all CDN image widgets on the page
     */
    function initAll() {
        document.querySelectorAll('.cdn-image-field:not([data-cdn-widget-init])').forEach(initCdnImageField);
        document.querySelectorAll('.cdn-image-list-field:not([data-cdn-widget-init])').forEach(initCdnImageListField);
    }

    // Inject styles
    function injectStyles() {
        if (document.getElementById('cdn-image-widget-styles')) return;
        const style = document.createElement('style');
        style.id = 'cdn-image-widget-styles';
        style.textContent = `
            .cdn-image-widget {
                display: flex;
                gap: 12px;
                align-items: flex-start;
            }
            .cdn-image-preview {
                width: 120px;
                height: 120px;
                border: 1px solid var(--border-color, #ccc);
                border-radius: 4px;
                display: flex;
                align-items: center;
                justify-content: center;
                background: var(--darkened-bg, #f8f8f8);
                overflow: hidden;
            }
            .cdn-image-preview img {
                max-width: 100%;
                max-height: 100%;
                object-fit: contain;
            }
            .cdn-image-no-preview {
                color: var(--body-quiet-color, #666);
                font-size: 12px;
            }
            .cdn-image-input-container {
                flex: 1;
                display: flex;
                flex-direction: column;
                gap: 8px;
            }
            .cdn-image-input-container input {
                width: 100%;
            }
            .cdn-image-buttons {
                display: flex;
                gap: 8px;
            }
            .cdn-image-btn {
                padding: 4px 12px;
                border: 1px solid var(--border-color, #ccc);
                border-radius: 4px;
                background: var(--body-bg, #fff);
                color: var(--body-fg, #333);
                cursor: pointer;
                font-size: 12px;
            }
            .cdn-image-btn:hover:not(:disabled) {
                background: var(--darkened-bg, #f0f0f0);
            }
            .cdn-image-btn:disabled {
                opacity: 0.5;
                cursor: not-allowed;
            }
            .cdn-image-btn.cdn-image-clear {
                color: var(--delete-button-bg, #ba2121);
            }
            .cdn-image-btn.cdn-image-random {
                background: var(--primary, #417690);
                color: white;
                border-color: var(--primary, #417690);
            }
            .cdn-image-btn.cdn-image-upload {
                background: var(--primary, #28a745);
                color: white;
                border-color: var(--primary, #28a745);
            }
            .cdn-image-btn.cdn-image-upload:hover:not(:disabled) {
                background: #218838;
            }

            /* Asset dropdown */
            .cdn-asset-dropdown {
                position: fixed;
                z-index: 10000;
                background: var(--body-bg, #fff);
                border: 1px solid var(--border-color, #ccc);
                border-radius: 4px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.15);
                max-height: 300px;
                overflow-y: auto;
                display: none;
            }
            .cdn-asset-item {
                display: flex;
                align-items: center;
                gap: 8px;
                padding: 8px 12px;
                cursor: pointer;
            }
            .cdn-asset-item:hover, .cdn-asset-item.selected {
                background: var(--darkened-bg, #f0f0f0);
            }
            .cdn-asset-item.no-results {
                color: var(--body-quiet-color, #666);
                font-style: italic;
                cursor: default;
            }
            .cdn-asset-item img {
                width: 32px;
                height: 32px;
                object-fit: contain;
                border-radius: 2px;
            }

            /* Image list widget */
            .cdn-image-list-widget {
                display: flex;
                flex-direction: column;
                gap: 12px;
                max-width: 600px;
            }
            .cdn-list-items {
                display: flex;
                flex-direction: column;
                gap: 8px;
            }
            .cdn-list-item {
                display: flex;
                gap: 12px;
                align-items: flex-start;
                padding: 8px;
                background: var(--darkened-bg, #f8f8f8);
                border: 1px solid var(--border-color, #ddd);
                border-radius: 4px;
            }
            .cdn-list-item-preview {
                width: 64px;
                height: 64px;
                min-width: 64px;
                border: 1px solid var(--border-color, #ccc);
                border-radius: 4px;
                display: flex;
                align-items: center;
                justify-content: center;
                background: var(--body-bg, #fff);
                overflow: hidden;
            }
            .cdn-list-item-preview img {
                max-width: 100%;
                max-height: 100%;
                object-fit: contain;
            }
            .cdn-list-item-input-container {
                flex: 1;
                display: flex;
                flex-direction: column;
                gap: 6px;
            }
            .cdn-list-item-input {
                width: 100%;
                padding: 6px 8px;
                border: 1px solid var(--border-color, #ccc);
                border-radius: 4px;
                font-size: 13px;
            }
            .cdn-list-item-buttons {
                display: flex;
                gap: 6px;
                flex-wrap: wrap;
            }
            .cdn-list-add-container {
                display: flex;
                align-items: center;
                gap: 12px;
                padding-top: 4px;
            }
            .cdn-list-counter {
                color: var(--body-quiet-color, #666);
                font-size: 12px;
            }
            .cdn-list-add-btn {
                padding: 6px 16px;
                background: var(--primary, #417690);
                color: white;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 13px;
            }
            .cdn-list-add-btn:hover:not(:disabled) {
                background: #205067;
            }
            .cdn-list-add-btn:disabled {
                opacity: 0.5;
                cursor: not-allowed;
            }

            /* Dark theme */
            [data-theme="dark"] .cdn-image-preview,
            [data-theme="dark"] .cdn-carousel-preview,
            [data-theme="dark"] .cdn-list-item-preview {
                background: var(--darkened-bg, #2d2d2d);
                border-color: var(--border-color, #444);
            }
            [data-theme="dark"] .cdn-asset-dropdown {
                background: var(--body-bg, #1e1e1e);
                border-color: var(--border-color, #444);
            }
            [data-theme="dark"] .cdn-list-item {
                background: var(--darkened-bg, #2d2d2d);
                border-color: var(--border-color, #444);
            }
            [data-theme="dark"] .cdn-list-item-input {
                background: var(--body-bg, #1e1e1e);
                color: var(--body-fg, #eee);
                border-color: var(--border-color, #444);
            }
        `;
        document.head.appendChild(style);
    }

    // Run on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            injectStyles();
            initAll();
        });
    } else {
        injectStyles();
        initAll();
    }

    // Re-init on Django admin inline add
    if (typeof django !== 'undefined' && django.jQuery) {
        django.jQuery(document).on('formset:added', function() {
            setTimeout(initAll, 100);
        });
    }

    // Export for manual init
    window.CdnImageWidget = { init: initAll };
})();
